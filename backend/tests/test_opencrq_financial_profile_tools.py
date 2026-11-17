"""Contract tests of the OpenCRQ financial profile tools.

Same shape as ``test_opencrq_recent_domain_tools.py``: ``_trpc`` is patched, the
procedures and the exact inputs are pinned against
``back/src/features/app-router/financial-profiles/financial-profiles-router.ts`` of the
OpenCRQ repository, and the patched ``_trpc`` answers with responses recorded from a
running OpenCRQ (``tests/data/opencrq``).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.api.v1.mcp_platform import OPENCRQ_MCP_READ_TOOLS, OPENCRQ_MCP_TOOLS
from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import build_tools, crq
from app.services.tool_classification import classify_tool_name
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.opencrq.io"
TOKEN = f"{BASE}||test-token"
DATA = Path(__file__).parent / "data" / "opencrq"

TENANT = "89c70e9a-8317-4da7-8765-1b9ba004c32c"
ACME_BANK = "90b57256-32e4-4369-b768-fa6f2bb64fee"

GET = "get_opencrq_financial_profile"
UPDATE = "update_opencrq_financial_profile"

ANSWER_KEYS = {q.key for q in crq._PROFILE_QUESTIONS}


def _fixture(procedure: str) -> Any:
    return json.loads((DATA / f"{procedure}.json").read_text(encoding="utf-8"))["data"]


PROFILE = _fixture("financialProfiles.getFinancialProfile")
ORGANIZATION = _fixture("organizations.getOrganization")


@contextlib.contextmanager
def _recorded_trpc(answers: dict[str, Any]):
    """Patch ``_trpc`` where the tools read through; yield the recorded calls.

    An answer that is a string starting with ``Error:`` is returned as the call's error.
    """
    calls: list[dict] = []

    async def fake(
        base_url, headers, procedure, action, *, input=None, mutation=False,
        tenant_id="", client=None,
    ):
        calls.append(
            {"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": tenant_id}
        )
        answer = answers.get(procedure)
        if isinstance(answer, str) and answer.startswith("Error:"):
            return None, answer
        return answer, None

    with patch.object(opencrq_common, "_trpc", fake), patch.object(crq, "_trpc", fake):
        yield calls


async def _execute(tool: str, /, **arguments):
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    return await executor.execute_tool(
        ToolCall(id="call-1", name=tool, arguments={"tenant_id": TENANT, **arguments})
    )


async def _run(tool: str, /, **arguments) -> str:
    result = await _execute(tool, **arguments)
    assert not result.is_error, result.content
    return result.content


def _sent(procedure: str, input: Any, mutation: bool = False) -> dict:
    return {"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": TENANT}


def _saved_answers(calls: list[dict]) -> dict:
    (save,) = [c for c in calls if c["procedure"] == "financialProfiles.saveFinancialProfile"]
    assert save["mutation"] is True
    assert save["input"]["organizationId"] == ACME_BANK
    return save["input"]["answers"]


# -- Registration and exposure --------------------------------------------------


def test_both_tools_are_registered_and_exposed_over_mcp():
    executor = CallableToolExecutor()
    names = {t.name for t in build_tools(TOKEN, executor)}
    assert {GET, UPDATE} <= names
    assert {GET, UPDATE} <= executor._handlers.keys()
    assert {GET, UPDATE} <= OPENCRQ_MCP_TOOLS
    assert GET in OPENCRQ_MCP_READ_TOOLS
    assert UPDATE not in OPENCRQ_MCP_READ_TOOLS


def test_the_update_is_classified_as_a_write_so_it_is_gated():
    assert classify_tool_name(GET)[0] == "read"
    assert classify_tool_name(UPDATE)[0] == "write"


def test_the_questionnaire_names_every_answer_opencrq_returns():
    answer_keys = set(PROFILE) - {"id", "organizationId", "coefficientSetVersion", "updatedAt"}
    assert answer_keys == ANSWER_KEYS


# -- Read -----------------------------------------------------------------------


async def test_read_contract():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": PROFILE,
    }
    with _recorded_trpc(answers) as calls:
        out = await _run(GET, organization_id=ACME_BANK)
    assert calls == [
        # organizations/organizations-router.ts: getOrganization input is {id}
        _sent("organizations.getOrganization", {"id": ACME_BANK}),
        _sent("financialProfiles.getFinancialProfile", {"organizationId": ACME_BANK}),
    ]
    for expected in (
        "Financial profile of Acme Bank:",
        f"Link: [Open in OpenCRQ]({BASE}/{TENANT}/{ACME_BANK}/details/overview)",
        "Revenue and margin:",
        "  Annual revenue (annual_revenue): 3,200,000,000 USD",
        "  Gross margin (gross_margin_pct): 42%",
        "  Operating days per year (operating_days_band): continuous_365",
        "  Time to full restore, backups intact (full_restore_band): 1_3d",
        "(records_held_gate): yes",
        "  Strictest regulatory regime that applies (regulatory_regime): dora",
        "  Ransom payment ceiling (ransom_payment_ceiling): 1,900,000 USD",
    ):
        assert expected in out, f"{expected!r} not in:\n{out}"
    assert "not answered" not in out
    for placeholder in ("N/A", "None", "?"):
        assert placeholder not in out, f"{placeholder!r} shown in:\n{out}"


async def test_a_profile_never_filled_says_so():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": None,
    }
    with _recorded_trpc(answers):
        out = await _run(GET, organization_id=ACME_BANK)
    assert out == "The financial profile of Acme Bank has not been filled in yet."


async def test_an_unknown_organization_is_not_read_as_an_empty_profile():
    with _recorded_trpc({"organizations.getOrganization": None}) as calls:
        out = await _run(GET, organization_id=ACME_BANK)
    assert out == f"Organization {ACME_BANK} not found."
    assert [c["procedure"] for c in calls] == ["organizations.getOrganization"]


# -- Update ---------------------------------------------------------------------


async def test_an_update_sends_back_every_answer_it_does_not_name():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": PROFILE,
        "financialProfiles.saveFinancialProfile": {},
    }
    with _recorded_trpc(answers) as calls:
        out = await _run(
            UPDATE, organization_id=ACME_BANK,
            answers={"annual_revenue": 2_500_000_000, "regulatory_regime": "nis2_essential"},
            clear=["ransom_payment_ceiling"],
        )
    assert [c["procedure"] for c in calls] == [
        "organizations.getOrganization",
        "financialProfiles.getFinancialProfile",
        "financialProfiles.saveFinancialProfile",
    ]
    saved = _saved_answers(calls)
    assert saved.keys() == ANSWER_KEYS
    assert saved == {
        **{key: PROFILE[key] for key in ANSWER_KEYS},
        "annualRevenue": 2_500_000_000,
        "regulatoryRegime": "nis2_essential",
        "ransomPaymentCeiling": None,
    }
    assert out == (
        "Saved the financial profile of Acme Bank: set annual_revenue, regulatory_regime; "
        "cleared ransom_payment_ceiling. OpenCRQ re-derives the loss estimates of its risk "
        "scenarios from it."
    )


async def test_a_first_answer_sends_every_other_question_blank():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": None,
        "financialProfiles.saveFinancialProfile": {},
    }
    with _recorded_trpc(answers) as calls:
        await _run(UPDATE, organization_id=ACME_BANK, answers={"annual_revenue": 1200})
    assert _saved_answers(calls) == {key: None for key in ANSWER_KEYS} | {"annualRevenue": 1200}


async def test_a_failed_read_saves_nothing():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": "Error: OpenCRQ is down",
    }
    with _recorded_trpc(answers) as calls:
        result = await _execute(UPDATE, organization_id=ACME_BANK, answers={"annual_revenue": 1})
    assert result.is_error
    assert result.content == "Error: OpenCRQ is down"
    assert "financialProfiles.saveFinancialProfile" not in [c["procedure"] for c in calls]


async def test_a_refused_save_is_reported():
    answers = {
        "organizations.getOrganization": ORGANIZATION,
        "financialProfiles.getFinancialProfile": PROFILE,
        "financialProfiles.saveFinancialProfile": "Error: Invalid option for regulatoryRegime",
    }
    with _recorded_trpc(answers):
        result = await _execute(UPDATE, organization_id=ACME_BANK, answers={"regulatory_regime": "gdpr"})
    assert result.is_error
    assert result.content == "Error: Invalid option for regulatoryRegime"


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"organization_id": "", "answers": {"annual_revenue": 1}}, "organization_id is required."),
        ({"organization_id": ACME_BANK}, "Give at least one answer to set or one question to clear."),
        (
            {"organization_id": ACME_BANK, "answers": {"annual_revenue": 1}, "clear": ["annual_revenue"]},
            "Both set and cleared: annual_revenue. Give each question one change.",
        ),
    ],
)
async def test_invalid_arguments_return_before_any_call(args, expected):
    with _recorded_trpc({}) as calls:
        out = await _run(UPDATE, **args)
    assert out == expected
    assert calls == []


async def test_an_unknown_question_is_refused_before_any_call():
    with _recorded_trpc({}) as calls:
        result = await _execute(UPDATE, organization_id=ACME_BANK, clear=["annual_turnover"])
    assert calls == []
    assert "annual_turnover" in result.content
