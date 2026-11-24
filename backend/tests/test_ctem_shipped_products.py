# SPDX-License-Identifier: Apache-2.0

"""CTEM: a vulnerability on a product the organization ships resolves to its repositories (#1432)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.services import ctem_case_loop as loop
from app.services import ctem_scope_gate as gate
from app.services import ctem_signal_listeners as listeners
from app.services import organization_context as oc
from app.services import organization_context_categories as categories
from app.services import organization_context_rows as rows
from app.services.organization_context_proposals import _contract_paths

TODAY = date(2026, 11, 23)
CVE = "CVE-2026-12345"


def _profile(**extra: object) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "tech_stack": [{"product": "PostgreSQL", "vendor": "PostgreSQL"}],
            "shipped_products": [
                {
                    "product": "Acme Server",
                    "vendor": "Acme",
                    "cpe": "cpe:2.3:a:acme:acme_server:*:*:*:*:*:*:*:*",
                    "repositories": ["acme/server", "https://github.com/acme/server-ui.git"],
                },
                {"product": "Acme Agent", "vendor": "Acme", "repositories": ["acme/agent"]},
            ],
            "assets_exposure": [
                {
                    "name": "acme/server",
                    "category": "source_code",
                    "subcategory": "code_repository",
                    "criticality": "very_high",
                }
            ],
            **extra,
        }
    )


def _facts(ref: str = CVE, kind: str = "vulnerability", **evidence: object) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind=kind,
        subject_ref=ref,
        source_provider="opencti",
        evidence=evidence,
    )


# ── the profile field ───────────────────────────────────────────────────


def test_repositories_are_owner_name_once() -> None:
    entry = _profile().shipped_products[0]
    assert entry.repositories == ["acme/server", "acme/server-ui"]
    with pytest.raises(ValidationError):
        oc.ShippedProduct(product="X", repositories=["not-a-repository"])


def test_tech_stack_is_unchanged_and_an_empty_list_dumps_as_before() -> None:
    profile = _profile()
    assert [e.product for e in profile.tech_stack] == ["PostgreSQL"]
    assert "shipped_products" not in oc.legacy_dump(oc.parse_profile({}))
    assert oc.legacy_dump(profile)["shipped_products"][0]["product"] == "Acme Server"


def test_shipped_products_is_a_category_a_row_section_and_proposable() -> None:
    assert categories.get("shipped_products") is not None
    assert "shipped_products" in rows.ROW_SECTIONS
    assert "shipped_products" in _contract_paths("primary")


def test_a_settings_save_without_shipped_products_keeps_the_stored_ones() -> None:
    import inspect

    from app.api.v1 import organization_context as api

    source = inspect.getsource(api.save_organization_context)
    assert '("fx_rates", "shipped_products", "pir_details")' in source


def test_the_index_names_shipped_products_only_once_filled() -> None:
    category = categories.get("shipped_products")
    assert category is not None
    assert not categories.indexed(category, 0) and categories.indexed(category, 2)
    tech_stack = categories.get("tech_stack")
    assert tech_stack is not None and categories.indexed(tech_stack, 0)


def test_rows_upsert_adds_a_new_product_and_keeps_an_existing_one() -> None:
    plan = rows.plan_upsert(
        _profile(),
        "shipped_products",
        "",
        [
            {"product": "Acme Agent", "repositories": ["acme/other"]},
            {"product": "Acme CLI", "repositories": ["acme/cli"]},
        ],
    )
    assert plan.added == ["Acme CLI"] and plan.unchanged == ["Acme Agent"]
    assert plan.profile is not None
    agent = next(e for e in plan.profile.shipped_products if e.product == "Acme Agent")
    assert agent.repositories == ["acme/agent"]


# ── the match ───────────────────────────────────────────────────────────


def test_match_by_cpe_any_version() -> None:
    entry = _profile().shipped_products[0]
    assert gate.shipped_match(entry, "cpe:2.3:a:acme:acme_server:6.4.2:*:*:*:*:*:*:*") == "cpe"
    assert gate.shipped_match(entry, "cpe:/a:acme:acme_server:6.4") == "cpe"
    assert gate.shipped_match(entry, "cpe:2.3:a:other:acme_server:6.4.2") == ""


def test_match_by_product_and_vendor() -> None:
    agent = _profile().shipped_products[1]
    assert gate.shipped_match(agent, "Acme Agent") == "product"
    assert gate.shipped_match(agent, "cpe:2.3:a:acme:acme_agent:1.0") == "product"
    assert gate.shipped_match(agent, "cpe:2.3:a:someone:acme_agent:1.0") == ""
    # The product named without its vendor is no match when the row has a vendor.
    assert gate.shipped_match(oc.ShippedProduct(product="Agent", vendor="Acme"), "Agent for Windows") == ""


def test_cve_with_a_shipped_cpe_resolves_to_the_repositories() -> None:
    decision = gate.decide(_profile(), _facts(cpe=["cpe:2.3:a:acme:acme_server:6.4.2:*:*:*:*:*:*:*"]), today=TODAY)
    reconcile = decision.record["reconcile"]
    assert decision.record["reason_code"] != "subject_unresolved"
    assert reconcile["repositories"] == ["acme/server", "acme/server-ui"]
    assert reconcile["shipped_products"][0] == {
        "product": "Acme Server",
        "by": "cpe",
        "repositories": ["acme/server", "acme/server-ui"],
    }
    # The repository asset the code import wrote gives the scope its criticality.
    assert reconcile["assets"] == ["acme/server"]
    assert decision.outcome == "case"


def test_a_cpe_match_wins_over_a_product_match() -> None:
    software = ["cpe:2.3:a:acme:acme_server:6.4.2", "Acme Agent"]
    resolution = gate.resolve(_profile(), _facts(software=software))
    assert [s["product"] for s in resolution.shipped] == ["Acme Server"]


def test_cve_on_a_shipped_product_named_by_name_resolves_by_product() -> None:
    resolution = gate.resolve(_profile(), _facts(software=["Acme Agent"]))
    assert resolution.shipped == [{"product": "Acme Agent", "by": "product", "repositories": ["acme/agent"]}]
    assert resolution.repositories == ["acme/agent"] and resolution.resolved


def test_no_match_stays_unresolved() -> None:
    decision = gate.decide(_profile(), _facts(cpe=["cpe:2.3:a:other:thing:1.0"]), today=TODAY)
    assert decision.record["reason_code"] == "subject_unresolved"
    assert decision.record["reconcile"]["repositories"] == []


def test_opencti_vulnerability_carries_its_cpes_as_evidence() -> None:
    row = next(r for r in listeners.LISTENERS if r.provider == "opencti" and r.object_types == ("vulnerability",))
    payload = {
        "type": "create",
        "data": {
            "type": "vulnerability",
            "id": "vulnerability--1",
            "name": CVE,
            "x_opencti_cpe": ["cpe:2.3:a:acme:acme_server:6.4.2"],
        },
    }
    signal = listeners._opencti_signal(row, payload)
    assert signal is not None and signal["evidence"]["cpe"] == ["cpe:2.3:a:acme:acme_server:6.4.2"]


def test_the_opencti_cve_and_the_repository_advisory_share_one_case_key() -> None:
    from types import SimpleNamespace

    world = SimpleNamespace(
        trigger_path="new_vulnerability",
        subject_kind="vulnerability",
        subject_ref=CVE,
        subject_label=CVE,
        evidence={"cpe": ["cpe:2.3:a:acme:acme_server:6.4.2"]},
    )
    advisory = SimpleNamespace(
        trigger_path="new_vulnerability",
        subject_kind="vulnerability",
        subject_ref=CVE,
        subject_label="Acme Server flaw",
        evidence={"repository": "acme/server", "assets": ["acme/server"]},
    )
    expected = f"patch:vulnerability/{CVE.lower()}"
    assert loop.fix_of(world).key == loop.fix_of(advisory).key == expected  # type: ignore[arg-type]
