"""get_openaev_injector_contract lists the contract's input fields.

Gap openaev-injector-contract-fields-not-exposed: the read showed id, payload,
platforms and ATT&CK ids but not the ``injector_contract_content`` fields, so an
agent read the injector's source code to build inject content.
"""

from __future__ import annotations

import json

from app.integrations.providers.openaev._common import (
    _fmt_arsenal_action,
    _fmt_contract_fields,
    _fmt_injector_contract,
)

_NMAP_CONTENT = {
    "contract_id": "0b7f3674-ac5d-4b95-b749-6665e74a211f",
    "fields": [
        {
            "key": "target_selector",
            "label": "Type of targets",
            "type": "select",
            "mandatory": True,
            "readOnly": False,
            "defaultValue": ["asset-groups"],
            "choices": {"assets": "Assets", "asset-groups": "Asset groups", "manual": "Manual"},
            "linkedFields": [],
        },
        {
            "key": "asset_groups",
            "label": "Targeted asset groups",
            "type": "asset-group",
            "mandatory": False,
            "cardinality": "n",
            "linkedFields": [{"key": "target_selector", "type": "select"}],
            "visibleConditionFields": ["target_selector"],
            "visibleConditionValues": {"target_selector": "asset-groups"},
            "mandatoryConditionFields": ["target_selector"],
            "mandatoryConditionValues": {"target_selector": "asset-groups"},
        },
        {
            "key": "target_property_selector",
            "label": "Targeted property",
            "type": "choice",
            "mandatory": True,
            "defaultValue": "automatic",
            "choices": [
                {"label": "Automatic", "value": "automatic", "information": ""},
                {"label": "Hostname", "value": "hostname", "information": ""},
            ],
            "mandatoryGroups": ["asset_groups", "assets"],
        },
        {
            "key": "expectations",
            "label": "Expectations",
            "type": "expectation",
            "availableExpectations": [{"expectation_type": "VULNERABILITY", "expectation_is_predefined": True}],
        },
    ],
}


def _contract(content: object) -> dict[str, object]:
    return {
        "injector_contract_id": "0b7f3674-ac5d-4b95-b749-6665e74a211f",
        "injector_contract_labels": {"en": "Nmap - SYN Scan"},
        "injector_contract_content": content,
    }


def test_contract_read_lists_fields_with_types_defaults_choices_and_conditions() -> None:
    out = _fmt_injector_contract(_contract(json.dumps(_NMAP_CONTENT)))

    assert "Input fields (4;" in out
    assert "- target_selector [select] Type of targets (mandatory)" in out
    assert 'default: ["asset-groups"]' in out
    assert "choices: assets (Assets), asset-groups (Asset groups), manual (Manual)" in out
    assert "- asset_groups [asset-group] Targeted asset groups (cardinality n)" in out
    assert "linked to: target_selector" in out
    assert "shown when: target_selector = asset-groups" in out
    assert "mandatory when: target_selector = asset-groups" in out
    assert "default: automatic" in out
    assert "choices: automatic (Automatic), hostname (Hostname)" in out
    assert "one of mandatory with: asset_groups, assets" in out
    assert "expectations offered: VULNERABILITY" in out


def test_content_already_decoded_is_read_too() -> None:
    out = _fmt_injector_contract(_contract(_NMAP_CONTENT))
    assert "- target_property_selector [choice]" in out


def test_absent_or_malformed_content_adds_no_line() -> None:
    for content in (None, "", "{not json", json.dumps({"fields": "x"}), json.dumps([])):
        out = _fmt_injector_contract(_contract(content))
        assert "Input fields" not in out
        assert out.startswith("[InjectorContract] Nmap - SYN Scan")


def test_long_lists_are_bounded_and_say_so() -> None:
    content = {
        "fields": [
            {"key": f"f{i}", "type": "select", "choices": {str(c): str(c) for c in range(30)}} for i in range(45)
        ]
    }
    lines = _fmt_contract_fields(content)
    assert lines[0].startswith("  Input fields (45;")
    assert "    ... 5 more field(s) not shown" in lines
    assert any(line.endswith("(+5 more)") for line in lines)


def test_arsenal_get_points_at_the_contract_read() -> None:
    out = _fmt_arsenal_action({"injector_contract_id": "abc-1", "action_name": "Nmap"}, detailed=True)
    assert "get_openaev_injector_contract(contract_id='abc-1')" in out
    summary = _fmt_arsenal_action({"injector_contract_id": "abc-1", "action_name": "Nmap"})
    assert "get_openaev_injector_contract" not in summary
