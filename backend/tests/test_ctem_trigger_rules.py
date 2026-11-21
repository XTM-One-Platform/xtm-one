# SPDX-License-Identifier: Apache-2.0
"""CTEM-08: "a vulnerability on software we run", read before any model call."""

from __future__ import annotations

from app.services import ctem_signal_listeners as listeners
from app.services import ctem_trigger_rules as rules
from app.services.ctem_scope_gate import runs_in_profile
from app.services.organization_context import OrganizationProfile


def _profile() -> OrganizationProfile:
    return OrganizationProfile.model_validate(
        {
            "tech_stack": [
                {"product": "nginx", "vendor": "f5"},
                {"product": "PostgreSQL", "cpe": "cpe:2.3:a:postgresql:postgresql:16.2:*:*:*:*:*:*:*"},
            ],
            "assets_exposure": [{"name": "edge-01.example.com", "category": "host"}],
        }
    )


def _has(source: str, *, nested: bool = True) -> dict:
    data = {
        "id": "relationship--1",
        "type": "relationship",
        "relationship_type": "has",
        "source_ref": "software--1",
        "target_ref": "vulnerability--1",
    }
    if nested:
        data["extensions"] = {
            "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba": {
                "source_value": source,
                "source_type": "Software",
                "target_type": "Vulnerability",
            }
        }
    else:
        data["source_value"] = source
    return {"type": "create", "data": data}


def test_rule_is_declared_by_the_trigger_config_only():
    assert rules.rule_of({"provider": "opencti", "runs_in_profile": "source"}) == "source"
    assert rules.rule_of({"provider": "opencti"}) == ""
    assert rules.rule_of({"runs_in_profile": "target"}) == ""
    assert rules.rule_of(None) == ""


def test_source_name_reads_the_opencti_extension_or_the_top_level():
    assert rules.source_name(_has("nginx 1.27.1")) == "nginx 1.27.1"
    assert rules.source_name(_has("nginx", nested=False)) == "nginx"
    assert rules.source_name({"data": {"type": "vulnerability"}}) == ""
    assert rules.source_name("x") == ""


def test_software_in_the_stack_starts_the_run():
    assert rules.drop_reason(_profile(), _has("nginx 1.27.1")) is None
    assert rules.drop_reason(_profile(), _has("cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*")) is None
    assert rules.drop_reason(_profile(), _has("cpe:2.3:a:postgresql:postgresql:16.2:*:*:*:*:*:*:*")) is None


def test_an_asset_of_the_profile_starts_the_run():
    assert rules.drop_reason(_profile(), _has("EDGE-01.example.com.")) is None


def test_software_outside_the_stack_is_dropped_with_its_name():
    reason = rules.drop_reason(_profile(), _has("Apache Tomcat 10.1"))
    assert reason is not None and "Apache Tomcat 10.1" in reason and reason.startswith("runs_in_profile:")


def test_no_source_or_no_profile_is_dropped():
    assert "no relationship source" in (rules.drop_reason(_profile(), {"data": {"type": "relationship"}}) or "")
    assert "not saved" in (rules.drop_reason(None, _has("nginx")) or "")


def test_a_name_that_only_contains_a_product_as_a_substring_does_not_match():
    assert runs_in_profile(_profile(), "nginxplus-fork") == []
    assert runs_in_profile(_profile(), "nginx") == ["nginx"]


def test_the_signal_of_a_has_relationship_carries_its_source_as_software():
    row, fields = listeners.match("opencti", "create", _has("nginx 1.27.1"))
    assert row.trigger_path == "new_vulnerability"
    assert fields["ref"] == "vulnerability--1"
    assert fields["evidence"]["software"] == ["nginx 1.27.1"]
