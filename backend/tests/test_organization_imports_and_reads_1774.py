# SPDX-License-Identifier: Apache-2.0

"""Organization imports and reads (#1774, harness batch x12-02).

1. A vulnerability on a product named like an imported repository resolves to that
   repository asset, with no typed ``shipped_products`` row.
2. A source refused for the software section names where its software comes in.
3. A code source's scope refusal reaches the preview as its reason (fixed by #1627, pinned).
4. An agent's assets query takes ``field:value`` terms, the CTEM Inventory list's filters.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import ctem_scope_gate as gate
from app.services import organization_context as oc
from app.services import organization_context_categories as cats
from app.services import organization_context_code as code
from app.services import organization_context_import as imp

TODAY = date(2026, 11, 24)


def _repo(name: str, *tags: str) -> dict[str, object]:
    return {"name": name, "category": "source_code", "subcategory": "code_repository", "tags": list(tags)}


def _profile(*assets: dict[str, object], shipped: list[dict[str, object]] | None = None) -> oc.OrganizationProfile:
    return oc.parse_profile({"assets_exposure": list(assets), "shipped_products": shipped or []})


def _facts(*software: str) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="vulnerability",
        subject_ref="CVE-2026-1774",
        source_provider="opencti",
        evidence={"software": list(software)},
    )


# ── 1. shipped products from the imported repositories ──────────────────


@pytest.mark.parametrize(
    "ref",
    ["cpe:2.3:a:acme:acme_server:6.4.2:*:*:*:*:*:*:*", "acme-server", "Acme Server", "ACME_SERVER"],
)
def test_a_reference_named_like_an_imported_repository_resolves_to_it(ref: str) -> None:
    resolution = gate.resolve(_profile(_repo("acme/acme-server")), _facts(ref))
    assert resolution.shipped == [{"product": "acme-server", "by": "repository", "repositories": ["acme/acme-server"]}]
    assert resolution.repositories == ["acme/acme-server"]
    assert [a.name for a in resolution.assets] == ["acme/acme-server"]
    assert resolution.resolved


def test_a_fork_or_another_asset_is_never_a_shipped_product() -> None:
    profile = _profile(
        _repo("someone/acme-server", "fork"),
        {"name": "acme-server", "category": "host"},
    )
    assert gate.resolve(profile, _facts("acme-server")).shipped == []
    assert not gate.repository_match(oc.AssetEntry.model_validate(_repo("acme/acme-server")), "acme")


def test_a_typed_row_keeps_its_match_and_the_repository_is_not_named_twice() -> None:
    profile = _profile(
        _repo("acme/server"),
        shipped=[{"product": "Server", "vendor": "", "repositories": ["acme/server"]}],
    )
    resolution = gate.resolve(profile, _facts("server"))
    assert [s["by"] for s in resolution.shipped] == ["product"]
    assert resolution.repositories == ["acme/server"]


def test_the_case_decision_names_the_repository() -> None:
    decision = gate.decide(_profile(_repo("acme/acme-agent")), _facts("cpe:/a:acme:acme_agent:1.0"), today=TODAY)
    assert decision.record["reason_code"] != "subject_unresolved"
    assert decision.record["reconcile"]["repositories"] == ["acme/acme-agent"]


# ── 2. the refusal of a section names where the software comes in ───────


@pytest.mark.parametrize("source", ["intune", "defender", "tanium", "github", "gitlab"])
def test_an_endpoint_or_code_source_refused_for_software_points_to_its_assets_import(source: str) -> None:
    message = imp.refused_section_message(source, "software")
    assert message.startswith(f"{source} cannot feed software: its software comes with the Assets import")
    reason = imp.readable_error(source, message)
    assert reason is not None and "Assets import" in reason and "CTEM Inventory Software tab" in reason
    assert reason.startswith(("Microsoft", "Tanium", "GitHub", "GitLab"))


def test_another_refusal_names_the_sections_the_source_feeds() -> None:
    assert imp.refused_section_message("opencrq", "pirs") == "opencrq cannot feed pirs: it feeds crown_jewels"
    assert imp.readable_error("opencrq", "opencrq cannot feed pirs: it feeds crown_jewels") == (
        "OpenCRQ cannot feed this section: it feeds crown_jewels."
    )


# ── 3. the code source scope refusal reaches the preview (#1627, pinned) ─


@pytest.mark.parametrize(("source", "label"), [("github", "GitHub"), ("gitlab", "GitLab")])
def test_a_code_source_scope_refusal_is_the_preview_reason(source: str, label: str) -> None:
    error = code.missing_scope_message(source, 403)
    plan = imp.ImportPlan(section="assets", source=source, source_total=None, read=0, error=error)
    reason = plan.as_dict()["error_reason"]
    assert reason is not None and reason.startswith(f"{label} refused the read (HTTP 403): grant the token")


# ── 4. field filters on the agent's assets read ─────────────────────────


def test_field_terms_are_filters_and_the_rest_is_the_name() -> None:
    filters, text = cats.split_asset_query("os_family:windows source:intune source:defender web Group:abc note:x")
    assert filters == {"os_family": ["windows"], "source": ["intune", "defender"], "group": ["abc"]}
    assert text == "web note:x"


def test_the_merged_page_names_the_next_call() -> None:
    rows = [{"name": "host-1", "sources": ["intune", "defender"], "merged_on": ["hostname"]}]
    page = cats.render_asset_page(rows, total=3, query="os_family:windows", offset=0, limit=1)
    assert "rows 1-1 of 3 matching 'os_family:windows'" in page
    assert "offset=1, limit=1" in page
    assert "no row" in cats.render_asset_page([], total=0, query="", offset=0, limit=50)
