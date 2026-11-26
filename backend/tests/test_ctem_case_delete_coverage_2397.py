"""A CTEM case delete turns its threat package's coverage off; a draft write on an object outside the draft is explained (#2397)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app.integrations.providers.opencti._common import _context_failure_message
from app.services import ctem_case_delete as case_delete

DRAFT = "52ad5359-0000-4000-8000-000000000000"
DRAFT_PAGE = f"https://ctem.octi.example/dashboard/data/import/draft/{DRAFT}"
GROUPING_PAGE = "https://ctem.octi.example/dashboard/analyses/groupings/fb93530a-0000-4000-8000-000000000000"
CRED = ("https://ctem.octi.example", {"Authorization": "Bearer t"})


def _package(url: str | None, coverage_id: str | None = "e28341aa") -> dict[str, Any]:
    return {"grouping_id": "fb93530a", "name": "Windows package", "url": url, "coverage_id": coverage_id, "at": None}


async def _retire(packages: list[dict[str, Any] | None], *, dry_run: bool) -> list[Any]:
    """The delete's coverage step on *packages*, as administrator ``u`` (no session: the access read is faked)."""
    actor = SimpleNamespace(id=uuid.uuid4(), email="admin@test.local")
    return await case_delete._retire_coverages(cast(Any, None), actor, packages, dry_run=dry_run)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every coverage write the delete sends: ``(coverage_id, draft)``, answered OK."""
    calls: list[tuple[str, str]] = []

    async def patch(base_url: str, headers: dict[str, str], coverage_id: str, draft_id: str) -> str | None:
        calls.append((coverage_id, draft_id))
        return None

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]]:
        return CRED

    monkeypatch.setattr(case_delete, "_patch_coverage", patch)
    monkeypatch.setattr("app.services.ctem_metrics_collector._user_creds", creds)
    return calls


async def test_a_real_delete_turns_the_coverage_off_in_the_package_draft(patched: list[tuple[str, str]]) -> None:
    [coverage] = await _retire([_package(DRAFT_PAGE)], dry_run=False)
    assert patched == [("e28341aa", DRAFT)]
    assert coverage["id"] == "e28341aa" and coverage["draft_id"] == DRAFT
    assert coverage["action"].startswith(case_delete.COVERAGE_DISABLED) and f"in draft {DRAFT}" in coverage["action"]
    deletion = case_delete.CaseDeletion(
        id="c",
        title="t",
        state="new",
        keys=[],
        opened_by="signal",
        severity=None,
        created_at=None,
        members=0,
        events=1,
        threat_package=_package(DRAFT_PAGE),
        coverage=coverage,
    )
    summary: dict[str, Any] = deletion.summary()["threat_package"]
    assert summary["coverage"]["action"] == coverage["action"]
    assert summary["grouping_id"] == "fb93530a", "the Grouping is kept and named (#2335)"


async def test_a_dry_run_names_the_coverage_and_calls_nothing(patched: list[tuple[str, str]]) -> None:
    [coverage] = await _retire([_package(DRAFT_PAGE)], dry_run=True)
    assert patched == []
    assert coverage == {"id": "e28341aa", "draft_id": DRAFT, "action": case_delete.COVERAGE_WOULD_DISABLE}


async def test_a_live_package_is_patched_live_and_a_gone_draft_falls_back_live(
    monkeypatch: pytest.MonkeyPatch, patched: list[tuple[str, str]]
) -> None:
    await _retire([_package(GROUPING_PAGE)], dry_run=False)
    assert patched == [("e28341aa", "")]
    patched.clear()

    async def draft_gone(base_url: str, headers: dict[str, str], coverage_id: str, draft_id: str) -> str | None:
        patched.append((coverage_id, draft_id))
        return "Error: the OpenCTI draft is no longer usable" if draft_id else None

    monkeypatch.setattr(case_delete, "_patch_coverage", draft_gone)
    [coverage] = await _retire([_package(DRAFT_PAGE)], dry_run=False)
    assert patched == [("e28341aa", DRAFT), ("e28341aa", "")]
    assert coverage["action"].endswith("(live)")


async def test_no_coverage_no_call_and_a_failure_is_named_not_raised(
    monkeypatch: pytest.MonkeyPatch, patched: list[tuple[str, str]]
) -> None:
    assert await _retire([_package(DRAFT_PAGE, None), None], dry_run=False) == [None, None]
    assert patched == []

    async def refused(*_: Any) -> str | None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(case_delete, "_patch_coverage", refused)
    [coverage] = await _retire([_package(GROUPING_PAGE)], dry_run=False)
    assert coverage["action"].startswith("not turned off, turn it off in OpenCTI")


async def test_no_opencti_access_is_named(monkeypatch: pytest.MonkeyPatch, patched: list[tuple[str, str]]) -> None:
    async def none(*_: Any) -> None:
        return None

    monkeypatch.setattr("app.services.ctem_metrics_collector._user_creds", none)
    [coverage] = await _retire([_package(DRAFT_PAGE)], dry_run=False)
    assert patched == [] and coverage["action"] == case_delete.COVERAGE_NO_ACCESS


async def test_the_coverage_write_sends_the_package_draft_and_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[dict[str, str], dict[str, Any]]] = []

    async def graphql(
        client: Any, base_url: str, headers: Any, query: str, variables: dict[str, Any]
    ) -> tuple[dict[str, Any], None]:
        sent.append((dict(headers), variables))
        assert "securityCoverageFieldPatch" in query
        return {"securityCoverageFieldPatch": {"id": variables["id"]}}, None

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", graphql)
    assert await case_delete._patch_coverage(CRED[0], {**CRED[1], "opencti-draft-id": "other"}, "cov", DRAFT) is None
    assert await case_delete._patch_coverage(CRED[0], {**CRED[1], "opencti-draft-id": "other"}, "cov", "") is None
    assert sent[0][0]["opencti-draft-id"] == DRAFT
    assert "opencti-draft-id" not in sent[1][0]
    assert sent[0][1]["input"] == [{"key": "auto_enrichment_disable", "value": [True]}]


def test_the_package_draft_is_read_from_its_page() -> None:
    assert case_delete._package_draft(DRAFT_PAGE) == DRAFT
    assert case_delete._package_draft(GROUPING_PAGE) == ""
    assert case_delete._package_draft(None) == ""


# ---------------------------------------------------------------------------
# A draft-scoped write on an object the draft does not hold
# ---------------------------------------------------------------------------

UPDATE = "mutation UpdateSDO($id: ID!, $input: [EditInput]!) { stixDomainObjectEdit(id: $id) { fieldPatch(input: $input) { id } } }"
NOT_FOUND = [
    {
        "message": "Cannot edit the field, Stix-Domain-Object cannot be found.",
        "extensions": {"code": "FUNCTIONAL_ERROR"},
    }
]


def test_an_object_missing_from_the_draft_is_explained() -> None:
    headers: dict[str, str] = {"opencti-draft-id": DRAFT}
    text = _context_failure_message(
        NOT_FOUND, None, headers, DRAFT, "UpdateSDO", "stixDomainObjectEdit", UPDATE, "error"
    )
    assert text.startswith("GraphQL error in 'UpdateSDO'") and "cannot be found" in text
    assert f"not visible in OpenCTI draft {DRAFT}" in text and "another draft" in text
    assert headers == {"opencti-draft-id": DRAFT}, "an object miss is not a dead draft: the draft stays"


def test_the_same_miss_live_or_a_dead_draft_is_not_explained_as_draft_scope() -> None:
    live = _context_failure_message(NOT_FOUND, None, {}, None, "UpdateSDO", "stixDomainObjectEdit", UPDATE, "error")
    assert "not visible in OpenCTI draft" not in live
    user = [{"message": "User cannot be found", "extensions": {"code": "FUNCTIONAL_ERROR"}}]
    text = _context_failure_message(user, None, {}, DRAFT, "UpdateSDO", "stixDomainObjectEdit", UPDATE, "error")
    assert "not visible in OpenCTI draft" not in text
