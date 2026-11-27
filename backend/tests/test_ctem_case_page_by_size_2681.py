"""A large case read stays under the tool-output cap and loses no issue (#2681).

``get_ctem_case`` paged its issues by count only, so a 320-issue upgrade case whose rows each
carry many assets and installed versions answered about 4x the 150,000-character cap and the
cut dropped issues mid-page. A page now stops filling under a character budget, each row's
lists are capped with a ``more_<field>`` count, ``issues_limit`` and ``issue_fields`` let the
caller ask for less, and the CVE rollups are computed on every row, whatever the page cut.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.services.builtin_tools import ctem_cases as tools_module

TOOL_CAP = 150_000


def _heavy_case(n: int = 320, assets: int = 40, versions: int = 30) -> dict[str, Any]:
    issues = []
    for i in range(n):
        issues.append(
            {
                "source": "opencti",
                "external_id": f"vulnerability--{i:04d}-{uuid.UUID(int=i)}",
                "name": f"CVE-2026-{10000 + i}",
                "cve": f"CVE-2026-{10000 + i}",
                "cvss": 5.0 + (i % 50) / 10,
                "epss": (i % 100) / 100,
                "kev": i % 37 == 0,
                "open": True,
                "assets": [f"workstation-{i:04d}-{a:03d}.corp.example" for a in range(assets)],
                "primary_assets": [f"primary-{a:03d}" for a in range(assets)],
                "primary_asset_links": [{"name": f"primary-{a:03d}", "basis": "proven"} for a in range(assets)],
                "version_outcomes": [
                    {"version": f"131.0.{v}", "outcome": "affected", "assets": v + 1, "fix": "132.0.1"}
                    for v in range(versions)
                ],
            }
        )
    return {
        "id": "c",
        "title": "Upgrade the browser",
        "issues": issues,
        "members_facts_unread": 113,
        "aggregates_restricted": False,
        "kev_count": None,
        "max_cvss": None,
        "max_epss": None,
    }


def _pages(rendered: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = tools_module._bounded_issues(rendered, offset or 0, **kw)
        pages.append(page)
        offset = page["issues_next_offset"]
        assert len(pages) < 400, "the pages never end"
    return pages


def test_every_page_of_a_heavy_case_stays_under_the_cap_and_serves_every_issue_once() -> None:
    rendered = _heavy_case()
    # The rows as stored would pass the cap on the first page of 100 alone.
    assert len(json.dumps(rendered["issues"][:100])) > TOOL_CAP
    pages = _pages(rendered, extra={"running_versions": {"versions": ["x" * 200] * 50}})
    for page in pages:
        assert len(tools_module._dump(page)) < TOOL_CAP
        assert len(tools_module._dump(page)) <= tools_module._ANSWER_BUDGET
    served = [row["external_id"] for page in pages for row in page["issues"]]
    assert served == [row["external_id"] for row in rendered["issues"]]
    assert all(page["issues_total"] == 320 for page in pages)
    assert pages[-1]["issues_next_offset"] is None and pages[-1]["page_cut_by"] is None


def test_a_page_cut_by_size_says_so_and_rows_carry_their_capped_lists() -> None:
    rendered = _heavy_case(n=320, assets=400, versions=200)
    first = tools_module._bounded_issues(rendered)
    assert first["page_cut_by"] == "size" and first["issues_next_offset"] == len(first["issues"])
    row = first["issues"][0]
    assert len(row["assets"]) == tools_module._ROW_LIST_CAPS["assets"] and row["more_assets"] == 380
    assert len(row["version_outcomes"]) == tools_module._ROW_LIST_CAPS["version_outcomes"]
    assert row["more_version_outcomes"] == 190
    # The stored rows are never changed by a page.
    assert len(rendered["issues"][0]["assets"]) == 400


def test_the_compact_fields_and_the_limit() -> None:
    rendered = _heavy_case()
    page = tools_module._bounded_issues(rendered, limit=100, fields="compact")
    assert len(page["issues"]) == 100 and page["issue_fields"] == "compact"
    assert all(
        set(row) == {"source", "external_id", "name", "cve", "cvss", "epss", "kev", "open"} for row in page["issues"]
    )
    assert page["page_cut_by"] == "count" and page["issues_next_offset"] == 100
    assert len(tools_module._bounded_issues(rendered, limit=7)["issues"]) == 7


def test_the_rollups_list_every_kev_cve_on_page_one_whatever_the_cut() -> None:
    rendered = _heavy_case()
    kev = sorted(row["cve"] for row in rendered["issues"] if row["kev"])
    first = tools_module._bounded_issues(rendered, limit=1)
    assert first["kev_cve_ids"] == kev and len(first["cve_ids"]) == 320
    assert first["lead_cve"] in kev
    # The signal facts are unread: the case facts stay unknown (#2225), the rows answer lower bounds.
    assert first["kev_count"] is None and first["max_cvss"] is None and first["max_epss"] is None
    assert first["rows_kev_count"] == len(kev)
    assert first["rows_max_cvss"] == max(r["cvss"] for r in rendered["issues"])
    assert "lower bounds" in first["rows_facts"]


def test_restricted_aggregates_stay_null() -> None:
    rendered = {**_heavy_case(n=10), "aggregates_restricted": True}
    page = tools_module._bounded_issues(rendered)
    assert page["kev_count"] is None and page["max_cvss"] is None
    assert "rows_kev_count" not in page and "rows_facts" not in page


def test_a_restricted_row_stays_restricted_in_a_compact_page() -> None:
    rendered = _heavy_case(n=3)
    rendered["issues"].append({"restricted": True})
    page = tools_module._bounded_issues(rendered, fields="compact")
    assert page["issues"][-1] == {"restricted": True}


def test_the_page_options_are_checked() -> None:
    assert tools_module.issue_page_options(0, "") == (100, "full", "")
    assert tools_module.issue_page_options(500, "COMPACT") == (100, "compact", "")
    assert tools_module.issue_page_options(-3, "full")[0] == 1
    assert tools_module.issue_page_options(10, "everything")[2]
    assert tools_module.issue_page_options("many", "full")[2]


def test_both_groups_offer_the_page_options() -> None:
    from app.services.builtin_tools.ctem_read import build_case_read

    read, _ = build_case_read(user_id=str(uuid.uuid4()), agent_id="")
    defs, _ = tools_module.build_ctem_case_tools(user_id=str(uuid.uuid4()), agent_id="")
    tools = next(d for d in defs if d.name == "get_ctem_case")
    for definition in (read, tools):
        assert {"issues_limit", "issue_fields"} <= set(definition.input_schema["properties"])
