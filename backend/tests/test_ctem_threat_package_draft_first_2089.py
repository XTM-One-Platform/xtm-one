"""The CTEM threat package is drafted before the analyst review answers (#2089).

- The draft gate: a run whose fleet gates the draft validation
  (``gated_draft_validation``, the CTEM analyst review) keeps the draft gate
  with the integration's Live writes on, so a package write auto-creates a
  draft in an autonomous run and never reaches the live graph first.
- The record-time guard: ``verify_package`` refuses a Grouping found in the
  live graph and in no open draft while the review has not approved the
  package (no gate yet, waiting, rejected, re-plan), whatever page is given.
- A refused review discards the draft: ``delete_opencti_draft`` is allowed on
  a gated fleet for the draft holding a case's recorded threat package
  (``case_draft_fn``), created in another run, and refused on any other open
  draft (an analyst's, another product's).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.agents.builtin_fleet_docs import _ctem_gates
from app.services import ctem_cases as rules
from app.services import ctem_threat_package_live as package_live

GROUPING = "8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"
OPEN_DRAFT = "7c6b5a49-3829-4176-a5b4-c3d2e1f0a9b8"
DRAFT_URL = f"https://cti.example.test/dashboard/data/import/draft/{OPEN_DRAFT}"


def _fake_opencti(monkeypatch: pytest.MonkeyPatch, *, live: set[str], drafts: dict[str, set[str]]) -> list[str]:
    sent: list[str] = []

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]]:
        return "https://cti.example.test", {"Authorization": "Bearer user-jwt"}

    async def graphql(
        client: Any, base_url: str, headers: Any, query: str, variables: Any = None, **_kw: Any
    ) -> tuple[dict[str, Any], str | None]:
        draft = headers.get(package_live._DRAFT_HEADER, "")
        if "draftWorkspaces" in query:
            sent.append("drafts")
            return {"draftWorkspaces": {"edges": [{"node": {"id": d}} for d in drafts]}}, None
        sent.append(f"grouping:{draft}")
        gid = variables["id"]
        found = (gid in drafts.get(draft, set())) if draft else (gid in live)
        return {"grouping": {"id": gid} if found else None}, None

    monkeypatch.setattr(package_live, "_user_creds", creds)
    monkeypatch.setattr(package_live, "_graphql", graphql)
    monkeypatch.setattr(package_live, "attach_probe", lambda headers, base_url: headers)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [None, "waiting", "rejected", "replan"])
@pytest.mark.parametrize("url", [DRAFT_URL, ""], ids=["draft-page", "no-page"])
async def test_live_grouping_recorded_before_approval_is_refused(
    monkeypatch: pytest.MonkeyPatch, review: str | None, url: str
) -> None:
    """The acceptance case: a package written live with no draft, review pending."""
    _fake_opencti(monkeypatch, live={GROUPING}, drafts={})
    package = {"grouping_id": GROUPING, "name": "CVE-2026-1234", "url": url}
    with pytest.raises(rules.CaseRuleError, match="create a draft first"):
        await package_live.verify_package(cast(Any, None), "u-1", package, review)


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [None, "waiting"])
async def test_grouping_held_in_an_open_draft_passes_before_approval(
    monkeypatch: pytest.MonkeyPatch, review: str | None
) -> None:
    """No regression for the runs that drafted: live and drafted (an edit) or drafted only."""
    _fake_opencti(monkeypatch, live={GROUPING}, drafts={OPEN_DRAFT: {GROUPING}})
    package = {"grouping_id": GROUPING, "url": DRAFT_URL}
    assert await package_live.verify_package(cast(Any, None), "u-1", package, review) == package
    _fake_opencti(monkeypatch, live=set(), drafts={OPEN_DRAFT: {GROUPING}})
    assert await package_live.verify_package(cast(Any, None), "u-1", package, review) == package


@pytest.mark.asyncio
async def test_approved_review_and_unplaced_grouping_are_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake_opencti(monkeypatch, live={GROUPING}, drafts={})
    package = {"grouping_id": GROUPING, "url": DRAFT_URL}
    assert await package_live.verify_package(cast(Any, None), "u-1", package, "approved") == package
    assert sent == []
    _fake_opencti(monkeypatch, live=set(), drafts={OPEN_DRAFT: set()})
    assert await package_live.verify_package(cast(Any, None), "u-1", package, "waiting") == package
    assert await package_live.verify_package(cast(Any, None), "u-1", {"name": "no grouping yet"}, None) == {
        "name": "no grouping yet"
    }


def _executor_and_tools(**kwargs: Any) -> tuple[Any, set[str]]:
    from app.integrations.providers.opencti.knowledge import register_knowledge
    from app.tools.base import CallableToolExecutor

    executor = CallableToolExecutor()
    defs = register_knowledge("https://cti.example.test", {}, executor, autonomous=True, **kwargs)
    return executor, {d.name for d in defs}


def test_live_writes_keep_the_draft_gate_on_a_fleet_that_gates_the_draft_validation() -> None:
    from app.integrations.providers.opencti._common import _DRAFT_REQUIRED_TOOLS

    gated, _ = _executor_and_tools(live_writes=True, gated_draft_validation=True)
    ungated, _ = _executor_and_tools(live_writes=True)
    name = "create_opencti_container"
    assert name in _DRAFT_REQUIRED_TOOLS
    # The gate wraps the handler (``__wrapped__`` points at the original); live writes alone do not.
    assert getattr(gated._handlers[name], "__wrapped__", None) is not None
    assert getattr(ungated._handlers[name], "__wrapped__", None) is None


@pytest.mark.asyncio
async def test_gated_fleet_may_delete_only_the_case_package_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused review discards the case's package draft from the answer run, and no other draft."""
    from app.integrations.providers.opencti import knowledge

    calls: list[str] = []

    async def graphql(client: Any, base_url: str, headers: Any, query: str, variables: Any = None, **_kw: Any):
        calls.append(query)
        if "draftWorkspaceDelete" in query:
            return {"draftWorkspaceDelete": OPEN_DRAFT}, None
        found = sum("draftWorkspaceDelete" in q for q in calls) == 0
        return {"draftWorkspace": {"id": OPEN_DRAFT, "name": "CVE-2026-1234"} if found else None}, None

    monkeypatch.setattr(knowledge, "_graphql", graphql)
    plain, _ = _executor_and_tools()
    refused = await plain._handlers["delete_opencti_draft"](draft_id=OPEN_DRAFT)
    assert "was not created in this run" in refused and calls == []
    # A gated fleet with no case lookup, or a draft no case records: refused, nothing sent.
    no_lookup, _ = _executor_and_tools(gated_draft_validation=True)
    assert "was not created in this run" in await no_lookup._handlers["delete_opencti_draft"](draft_id=OPEN_DRAFT)
    asked: list[str] = []

    async def not_a_case_draft(did: str) -> bool:
        asked.append(did)
        return False

    other, _ = _executor_and_tools(gated_draft_validation=True, case_draft_fn=not_a_case_draft)
    assert "was not created in this run" in await other._handlers["delete_opencti_draft"](draft_id=OPEN_DRAFT)
    assert asked == [OPEN_DRAFT] and calls == []

    async def case_draft(did: str) -> bool:
        return did == OPEN_DRAFT

    # The lookup only widens a gated fleet: without the gate it is never consulted.
    ungated, _ = _executor_and_tools(case_draft_fn=case_draft)
    assert "was not created in this run" in await ungated._handlers["delete_opencti_draft"](draft_id=OPEN_DRAFT)
    assert calls == []
    gated, _ = _executor_and_tools(gated_draft_validation=True, case_draft_fn=case_draft)
    answer = await gated._handlers["delete_opencti_draft"](draft_id=OPEN_DRAFT)
    assert "deleted" in answer and "Error" not in answer


@pytest.mark.asyncio
async def test_case_package_draft_lookup_refuses_a_non_uuid_without_reading() -> None:
    """A draft id that is not a UUID is never a case's package draft (no query, no LIKE pattern)."""
    assert await package_live.is_case_package_draft(cast(Any, None), "%") is False
    assert await package_live.is_case_package_draft(cast(Any, None), "") is False


def test_review_gate_says_what_each_answer_does_to_the_package() -> None:
    gate = next(g for g in _ctem_gates(launch_tools=True, mobilize_action="m") if g["key"] == "ctem.analyst_review")
    action = gate["action"]
    assert len(action) <= 200  # the fleet schema's bound on a gate action
    assert "nothing live before approval" in action
    assert "approve publishes it and sends its coverage to OpenAEV" in action
    assert "reject deletes the draft" in action
    assert "not publishing keeps it unvalidated" in action
