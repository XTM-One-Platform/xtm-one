"""An ask that links an OpenCTI draft links that draft alone for its platform (#1453).

An object an agent creates inside an OpenCTI draft (the CTEM threat package's
Grouping) has no live page until a person validates the draft. A decision ask
that carries the draft page and the object page next to it showed a broken
button: ``normalize_ask`` now drops the object pages of the platform a draft
link names, and keeps every other link.
"""

from __future__ import annotations

from app.services.decision_ask import drop_links_shadowed_by_drafts, normalize_ask

CTI = "https://cti.example.test"
DRAFT = f"{CTI}/dashboard/data/import/draft/0f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f"
GROUPING = f"{CTI}/dashboard/analyses/groupings/8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"


def _ask(*links: dict[str, str]) -> dict[str, object]:
    return {
        "question": "Is the threat package ready for its security coverage to be requested?",
        "options": [{"key": "A", "label": "Approve"}, {"key": "B", "label": "Revise"}],
        "recommendation": "A",
        "links": list(links),
    }


def test_drafted_package_ask_keeps_only_the_draft_link() -> None:
    ask = normalize_ask(_ask({"label": "Grouping (draft)", "url": GROUPING}, {"label": "OpenCTI draft", "url": DRAFT}))
    assert ask is not None
    assert ask["links"] == [{"label": "OpenCTI draft", "url": DRAFT}]


def test_validated_package_ask_keeps_the_grouping_link() -> None:
    ask = normalize_ask(_ask({"label": "Grouping", "url": GROUPING}))
    assert ask is not None
    assert ask["links"] == [{"label": "Grouping", "url": GROUPING}]


def test_id_resolver_page_of_the_same_platform_is_dropped_too() -> None:
    resolver = {"label": "Object", "url": f"{CTI}/dashboard/id/8a3e2f10-4c5d-4b6e-9f70-1a2b3c4d5e6f"}
    assert drop_links_shadowed_by_drafts([resolver, {"label": "Draft", "url": DRAFT}]) == [
        {"label": "Draft", "url": DRAFT}
    ]


def test_other_platforms_and_other_sources_are_kept() -> None:
    other = {"label": "Other OpenCTI", "url": "https://other.example.test/dashboard/analyses/groupings/x"}
    jira = {"label": "ITS-1", "url": "https://acme.atlassian.net/browse/ITS-1"}
    draft = {"label": "Draft", "url": DRAFT}
    assert drop_links_shadowed_by_drafts([other, jira, draft]) == [other, jira, draft]


def test_base_path_is_part_of_the_platform() -> None:
    draft = {"label": "Draft", "url": f"{CTI}/cti/dashboard/data/import/draft/0f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f"}
    root = {"label": "Root platform", "url": GROUPING}
    shadowed = {"label": "Same base", "url": f"{CTI}/cti/dashboard/analyses/groupings/abc"}
    assert drop_links_shadowed_by_drafts([root, shadowed, draft]) == [root, draft]
