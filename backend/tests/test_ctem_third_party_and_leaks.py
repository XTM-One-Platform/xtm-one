# SPDX-License-Identifier: Apache-2.0

"""CTEM-29 (#1099): third-party events (path 11) and credential leaks (path 10) as signals and cases."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services import ctem_scope_gate as gate
from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signals, organization_events
from app.services import organization_context as oc

TODAY = date(2026, 11, 22)


def _profile(**extra: Any) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "primary_assets": [
                {"name": "Customer SaaS", "criticality": "critical", "asset_names": ["Acme Hosting"]},
            ],
            "assets_exposure": [
                {"name": "Acme Hosting", "category": "saas_application", "criticality": "high", "source": "manual"},
            ],
            **extra,
        }
    )


def _facts(path: str, kind: str, ref: str, **evidence: Any) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path=path,
        lens="world",
        subject_kind=kind,
        subject_ref=ref,
        source_provider="opencti",
        evidence=evidence,
    )


# ── path 11: third-party events ─────────────────────────────────────────


def test_a_tracked_organization_a_primary_asset_depends_on_opens_a_case() -> None:
    decision = gate.decide(_profile(), _facts("third_party_event", "third_party", "Acme Hosting"), today=TODAY)
    assert decision.outcome == "case"
    assert decision.record["scope"]["criticality"] == "critical"
    # Named in the evidence, never a recorded link: inferred (#2396).
    assert {
        "from": "primary_asset",
        "name": "Customer SaaS",
        "criticality": "critical",
        "basis": "inferred",
    } in decision.record["scope"]["sources"]
    assert decision.record["scope"]["primary_asset_basis"] == "inferred"


def test_a_primary_asset_named_in_the_evidence_is_the_dependency() -> None:
    profile = oc.parse_profile({"primary_assets": [{"name": "Customer SaaS", "criticality": "critical"}]})
    decision = gate.decide(
        profile,
        _facts("third_party_event", "third_party", "Beta Mail", primary_assets=["Customer SaaS"]),
        today=TODAY,
    )
    assert decision.outcome == "case"


def test_a_tracked_organization_with_no_dependency_goes_to_the_watchlist() -> None:
    decision = gate.decide(_profile(), _facts("third_party_event", "third_party", "Unrelated Ltd"), today=TODAY)
    assert decision.outcome == "watchlist"
    assert decision.record["reason_code"] == "no_dependency"


def test_only_an_identity_match_names_a_tracked_organization() -> None:
    entries = [
        {"id": "org-a", "identity_ids": ["identity--a"], "sector_ids": ["identity--sector"]},
        {"id": "org-b", "identity_ids": ["identity--b"], "sector_ids": ["identity--sector"]},
    ]
    data = {"id": "report--1", "type": "report", "object_refs": ["identity--a", "identity--sector"]}
    assert organization_events.named_organizations(data, entries) == ["org-a"]
    sector_only = {"id": "report--2", "type": "report", "object_refs": ["identity--sector"]}
    assert organization_events.named_organizations(sector_only, entries) == []


# ── path 10: credential leaks ───────────────────────────────────────────


def test_a_leaked_identity_of_an_owned_domain_is_in_scope_at_the_policy_criticality() -> None:
    profile = _profile(owned_domains=["@Corp.Example."])
    assert profile.owned_domains == ["corp.example"]
    decision = gate.decide(profile, _facts("credential_leak", "identity", "jdoe@eu.corp.example"), today=TODAY)
    assert decision.outcome == "case"
    assert decision.record["scope"]["sources"] == [
        {"from": "owned_domain", "name": "corp.example", "criticality": "high"}
    ]
    low = _profile(owned_domains=["corp.example"], prioritization_policy={"identity_criticality": "low"})
    assert gate.decide(low, _facts("credential_leak", "identity", "jdoe@corp.example"), today=TODAY).outcome == (
        "watchlist"
    )


def test_a_leaked_identity_outside_the_owned_domains_is_to_validate() -> None:
    profile = _profile(owned_domains=["corp.example"])
    decision = gate.decide(profile, _facts("credential_leak", "identity", "jdoe@notcorp.example"), today=TODAY)
    assert decision.outcome == "to_validate"


def test_a_leaked_credential_of_a_disabled_account_is_closed_not_exposed() -> None:
    profile = _profile(owned_domains=["corp.example"])
    for evidence in ({"account_disabled": True}, {"account_enabled": False}):
        decision = gate.decide(
            profile, _facts("credential_leak", "identity", "jdoe@corp.example", **evidence), today=TODAY
        )
        assert decision.outcome == "discarded"
        assert decision.record["reason_code"] == "account_disabled"
        assert "not exposed" in decision.reason


def test_a_leak_signal_never_stores_the_secret() -> None:
    clean = ctem_signals.normalize(
        ctem_signals.SignalInput(
            trigger_path="credential_leak",
            source_provider="manual",
            subject_kind="identity",
            subject_ref="jdoe@corp.example",
            evidence={"password": "hunter2", "nested": [{"token": "abc"}], "source_url": "https://leak.example/1"},
        )
    )
    text = repr(clean.evidence)
    assert "hunter2" not in text and "'abc'" not in text
    assert clean.evidence["password_fingerprint"] == ctem_signals.fingerprint("hunter2")
    assert clean.evidence["nested"][0]["token_fingerprint"].startswith("sha256:")
    assert clean.evidence["source_url"] == "https://leak.example/1"
    # Another path keeps its evidence as given.
    other = ctem_signals.normalize(
        ctem_signals.SignalInput(
            trigger_path="new_exposure",
            source_provider="manual",
            subject_kind="asset",
            subject_ref="shop.example.com",
            evidence={"token": "visible"},
        )
    )
    assert other.evidence == {"token": "visible"}


def test_an_opencti_account_observable_records_a_leak_on_the_identity() -> None:
    payload = {
        "type": "create",
        "data": {
            "id": "user-account--1",
            "type": "user-account",
            "account_login": "JDoe@Corp.Example",
            "credential": "hunter2",
            "is_disabled": True,
        },
    }
    found = listeners.match("opencti", "create", payload)
    assert found is not None
    row, fields = found
    assert (row.trigger_path, row.subject_kind) == ("credential_leak", "identity")
    assert fields["ref"] == "jdoe@corp.example"
    assert fields["evidence"]["account_disabled"] is True
    assert "hunter2" not in repr(fields)


def test_an_address_observable_records_a_leak_and_one_without_a_domain_none() -> None:
    address = {"type": "create", "data": {"id": "email-addr--1", "type": "email-addr", "value": "ops@corp.example"}}
    found = listeners.match("opencti", "create", address)
    assert found is not None and found[1]["ref"] == "ops@corp.example"
    bare = {"type": "create", "data": {"id": "user-account--2", "type": "user-account", "account_login": "root"}}
    assert listeners.match("opencti", "create", bare) is None


def test_both_paths_have_a_listener_now() -> None:
    assert {"credential_leak"} <= listeners.paths_with_listener()


def test_owned_domains_stay_out_of_the_always_on_block() -> None:
    profile = _profile(owned_domains=["corp.example"])
    assert "owned_domains" not in oc.legacy_dump(profile)
    assert "owned_domains" not in dict(oc.CONTRACT_FIELDS)
