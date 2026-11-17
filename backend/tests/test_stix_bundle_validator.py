import json
import uuid

import pytest

from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.tools.base import ToolCall


@pytest.mark.asyncio
async def test_stix_bundle_validator_accepts_valid_bundle():
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "id": f"campaign--{uuid.uuid4()}",
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            }
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    assert not result.is_error
    payload = json.loads(result.content)

    assert payload["valid"] is True
    assert payload["error_count"] == 0
    assert payload["bundle"]["id"].startswith("bundle--")


@pytest.mark.asyncio
async def test_stix_bundle_validator_accepts_bundle_with_relationships():
    campaign_id = f"campaign--{uuid.uuid4()}"
    malware_id = f"malware--{uuid.uuid4()}"

    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "id": campaign_id,
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": malware_id,
                "name": "malware",
                "is_family": False,
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "relationship",
                "spec_version": "2.1",
                "id": f"relationship--{uuid.uuid4()}",
                "relationship_type": "uses",
                "source_ref": campaign_id,
                "target_ref": malware_id,
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    assert not result.is_error
    payload = json.loads(result.content)

    assert payload["valid"] is True
    assert payload["error_count"] == 0


@pytest.mark.asyncio
async def test_stix_bundle_validator_returns_fix_instructions_on_invalid_payload():
    # malware missing required `is_family` field — stix2 will reject this
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": f"malware--{uuid.uuid4()}",
                "name": "BadMalware",
                "malware_types": ["backdoor"],
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
                # missing required 'is_family'
            }
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    assert not result.is_error
    payload = json.loads(result.content)

    assert payload["valid"] is False
    assert payload["error_count"] >= 1
    assert isinstance(payload["errors"], list)


@pytest.mark.asyncio
async def test_stix_bundle_validator_rejects_empty_bundle_objects():
    # indicator with invalid STIX pattern syntax — stix2 will reject this
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "indicator",
                "spec_version": "2.1",
                "id": f"indicator--{uuid.uuid4()}",
                "name": "BadIndicator",
                "indicator_types": ["malicious-activity"],
                "pattern": "[invalid syntax here]",
                "pattern_type": "stix",
                "valid_from": "2022-11-25T19:00:05.000Z",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            }
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    assert not result.is_error
    payload = json.loads(result.content)

    assert payload["valid"] is False
    assert payload["error_count"] >= 1
    assert isinstance(payload["errors"], list)


@pytest.mark.asyncio
async def test_stix_bundle_validator_autofixes_uuid_errors_and_revalidates():
    broken_campaign_id = "campaign--not-a-uuid"
    bundle = {
        "type": "bundle",
        "id": "bundle--broken",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "id": broken_campaign_id,
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "indicator",
                "spec_version": "2.1",
                "id": "indicator--broken",
                "name": "GoodIndicator",
                "indicator_types": ["malicious-activity"],
                "pattern": "[ipv4-addr:value = '1.2.3.4']",
                "pattern_type": "stix",
                "valid_from": "2022-11-25T19:00:05.000Z",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "relationship",
                "spec_version": "2.1",
                "id": "relationship--broken",
                "relationship_type": "attributed-to",
                "source_ref": broken_campaign_id,
                "target_ref": "indicator--broken",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    assert not result.is_error
    payload = json.loads(result.content)

    assert payload["valid"] is True
    assert payload["error_count"] == 0
    assert payload.get("uuid_rewrites", 0) >= 1
    assert payload["bundle"]["id"].startswith("bundle--")

    objects = payload["bundle"]["objects"]
    campaign = next(obj for obj in objects if obj["type"] == "campaign")
    relationship = next(obj for obj in objects if obj["type"] == "relationship")
    assert campaign["id"].startswith("campaign--")
    assert relationship["source_ref"] == campaign["id"]


@pytest.mark.asyncio
async def test_stix_bundle_validator_rewrites_all_uuids_deterministically():
    """Verify that the same content always produces the same deterministic ID."""
    bundle = {
        "type": "bundle",
        "id": "bundle--whatever",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "id": "campaign--random-1",
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    result1 = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )
    # Run again with a different original ID but same content
    bundle["objects"][0]["id"] = "campaign--random-2"
    result2 = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )

    p1 = json.loads(result1.content)
    p2 = json.loads(result2.content)
    assert p1["valid"] is True
    assert p2["valid"] is True
    # Same content → same deterministic ID
    assert p1["bundle"]["objects"][0]["id"] == p2["bundle"]["objects"][0]["id"]


# ── TLP markings ────────────────────────────────────────────────────

TLP_SPEC_IDS = {
    "white": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    "green": "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    "amber": "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    "red": "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}
TLP_SPEC_CREATED = "2017-01-20T00:00:00.000Z"


def _bundle_with_marking(marking: dict) -> dict:
    """A minimal, otherwise-valid bundle whose indicator carries `marking`."""
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            marking,
            {
                "type": "indicator",
                "spec_version": "2.1",
                "id": f"indicator--{uuid.uuid4()}",
                "name": "GoodIndicator",
                "indicator_types": ["malicious-activity"],
                "pattern": "[ipv4-addr:value = '1.2.3.4']",
                "pattern_type": "stix",
                "valid_from": "2022-11-25T19:00:05.000Z",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
                "object_marking_refs": [marking["id"]],
            },
        ],
    }


async def _validate(bundle: dict, executor=None) -> dict:
    if executor is None:
        _, executor = build_stix_bundle_validator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="stix_bundle_validator",
            arguments={"bundle": bundle},
        )
    )
    assert not result.is_error
    return json.loads(result.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("color", ["white", "green", "amber", "red"])
async def test_stix_bundle_validator_keeps_spec_tlp_marking_ids(color: str):
    """The four spec-frozen TLP ids must survive the validator untouched.

    Renumbering them made python-stix2 reject the bundle on every single call,
    an unwinnable loop the model could not escape by editing the bundle.
    """
    spec_id = TLP_SPEC_IDS[color]
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": spec_id,
            "created": TLP_SPEC_CREATED,
            "definition_type": "tlp",
            "name": f"TLP:{color.upper()}",
            "definition": {"tlp": color},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    objects = payload["bundle"]["objects"]
    marking = next(obj for obj in objects if obj["type"] == "marking-definition")
    indicator = next(obj for obj in objects if obj["type"] == "indicator")
    assert marking["id"] == spec_id
    assert marking["definition"] == {"tlp": color}
    assert indicator["object_marking_refs"] == [spec_id]


@pytest.mark.asyncio
async def test_stix_bundle_validator_normalizes_opencti_tlp_clear():
    """OpenCTI's TLP:CLEAR validates, emitted as the spec white marking."""
    spec_id = TLP_SPEC_IDS["white"]
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": spec_id,
            "created": "2024-05-06T08:00:00.000Z",  # OpenCTI's own creation date
            "definition_type": "tlp",
            "name": "TLP:CLEAR",
            "definition": {"tlp": "clear"},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    marking = next(
        obj for obj in payload["bundle"]["objects"] if obj["type"] == "marking-definition"
    )
    assert marking["id"] == spec_id
    assert marking["definition"] == {"tlp": "white"}
    assert marking["created"] == TLP_SPEC_CREATED
    # The OpenCTI-facing name is not part of the spec check and is preserved.
    assert marking["name"] == "TLP:CLEAR"
    assert payload["marking_normalizations"] >= 1


@pytest.mark.asyncio
async def test_stix_bundle_validator_normalizes_uppercase_opencti_tlp_spelling():
    """`definition_type: "TLP"` + `definition: {"TLP": "TLP:AMBER"}` is understood."""
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": TLP_SPEC_IDS["amber"],
            "created": TLP_SPEC_CREATED,
            "definition_type": "TLP",
            "name": "TLP:AMBER",
            "definition": {"TLP": "TLP:AMBER"},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    marking = next(
        obj for obj in payload["bundle"]["objects"] if obj["type"] == "marking-definition"
    )
    assert marking["id"] == TLP_SPEC_IDS["amber"]
    assert marking["definition_type"] == "tlp"
    assert marking["definition"] == {"tlp": "amber"}


@pytest.mark.asyncio
async def test_stix_bundle_validator_repins_wrong_tlp_id_and_remaps_refs():
    """A TLP marking carrying a non-spec id is re-pinned, references follow."""
    wrong_id = "marking-definition--784ee1ca-e493-557d-949f-2b7415f2d906"
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": wrong_id,
            "created": TLP_SPEC_CREATED,
            "definition_type": "tlp",
            "name": "TLP:GREEN",
            "definition": {"tlp": "green"},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    objects = payload["bundle"]["objects"]
    marking = next(obj for obj in objects if obj["type"] == "marking-definition")
    indicator = next(obj for obj in objects if obj["type"] == "indicator")
    assert marking["id"] == TLP_SPEC_IDS["green"]
    assert indicator["object_marking_refs"] == [TLP_SPEC_IDS["green"]]


@pytest.mark.asyncio
async def test_stix_bundle_validator_collapses_tlp_clear_and_white_duplicates():
    """CLEAR and WHITE are the same marking; only one may carry the spec id."""
    spec_id = TLP_SPEC_IDS["white"]
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "marking-definition",
                "spec_version": "2.1",
                "id": spec_id,
                "created": TLP_SPEC_CREATED,
                "definition_type": "tlp",
                "name": "TLP:CLEAR",
                "definition": {"tlp": "clear"},
            },
            {
                "type": "marking-definition",
                "spec_version": "2.1",
                "id": spec_id,
                "created": TLP_SPEC_CREATED,
                "definition_type": "tlp",
                "name": "TLP:WHITE",
                "definition": {"tlp": "white"},
            },
        ],
    }

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    markings = [
        obj for obj in payload["bundle"]["objects"] if obj["type"] == "marking-definition"
    ]
    assert len(markings) == 1
    assert markings[0]["id"] == spec_id


@pytest.mark.asyncio
async def test_stix_bundle_validator_keeps_valid_object_ids_untouched():
    """Valid ids are identity: only malformed ones are repaired."""
    campaign_id = f"campaign--{uuid.uuid4()}"
    relationship_id = f"relationship--{uuid.uuid4()}"
    malware_id = f"malware--{uuid.uuid4()}"
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "id": campaign_id,
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": malware_id,
                "name": "malware",
                "is_family": False,
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
            {
                "type": "relationship",
                "spec_version": "2.1",
                "id": relationship_id,
                "relationship_type": "uses",
                "source_ref": campaign_id,
                "target_ref": malware_id,
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            },
        ],
    }

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    assert "uuid_rewrites" not in payload
    ids = [obj["id"] for obj in payload["bundle"]["objects"]]
    assert ids == [campaign_id, malware_id, relationship_id]
    relationship = payload["bundle"]["objects"][2]
    assert relationship["source_ref"] == campaign_id
    assert relationship["target_ref"] == malware_id


@pytest.mark.asyncio
async def test_stix_bundle_validator_goes_terminal_on_repeated_identical_error():
    """The same unfixable error three times in a row ends the fix-and-retry loop."""
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": f"malware--{uuid.uuid4()}",
                "name": "BadMalware",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
                # missing required 'is_family'
            }
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    first = await _validate(bundle, executor)
    second = await _validate(bundle, executor)
    third = await _validate(bundle, executor)

    assert first["valid"] is False
    assert first.get("terminal") is not True
    assert "call stix_bundle_validator again" in first["next_step"]
    assert second.get("terminal") is not True
    assert third["terminal"] is True
    assert third["repeat_count"] == 3
    assert "Do NOT call stix_bundle_validator" in third["next_step"]


@pytest.mark.asyncio
async def test_stix_bundle_validator_repeat_guard_resets_on_a_different_error():
    """Progress — a different error — restarts the budget, it is not a loop."""
    missing_is_family = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": f"malware--{uuid.uuid4()}",
                "name": "BadMalware",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
                # missing required 'is_family'
            }
        ],
    }
    broken_pattern = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "indicator",
                "spec_version": "2.1",
                "id": f"indicator--{uuid.uuid4()}",
                "name": "BadIndicator",
                "indicator_types": ["malicious-activity"],
                "pattern": "[invalid syntax here]",
                "pattern_type": "stix",
                "valid_from": "2022-11-25T19:00:05.000Z",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            }
        ],
    }

    _, executor = build_stix_bundle_validator_tool()
    await _validate(missing_is_family, executor)
    await _validate(missing_is_family, executor)
    third = await _validate(broken_pattern, executor)

    assert third["valid"] is False
    assert third.get("terminal") is not True
    assert third["next_step"].startswith("Fix all errors")


@pytest.mark.asyncio
async def test_stix_bundle_validator_leaves_statement_markings_alone():
    """A statement marking named like a TLP one keeps its id and its body."""
    statement_id = f"marking-definition--{uuid.uuid4()}"
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": statement_id,
            "created": "2024-05-06T08:00:00.000Z",
            "definition_type": "statement",
            "name": "TLP:RED equivalent",
            "definition": {"statement": "Internal use only"},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    objects = payload["bundle"]["objects"]
    marking = next(obj for obj in objects if obj["type"] == "marking-definition")
    indicator = next(obj for obj in objects if obj["type"] == "indicator")
    assert marking["id"] == statement_id
    assert marking["definition"] == {"statement": "Internal use only"}
    assert marking["created"] == "2024-05-06T08:00:00.000Z"
    assert indicator["object_marking_refs"] == [statement_id]


@pytest.mark.asyncio
async def test_stix_bundle_validator_normalizes_tlp_without_definition_type():
    """A `tlp` definition body identifies the marking on its own."""
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": f"marking-definition--{uuid.uuid4()}",
            "created": "2024-05-06T08:00:00.000Z",
            "name": "Clear",
            "definition": {"tlp": "clear"},
            # no 'definition_type' at all
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    objects = payload["bundle"]["objects"]
    marking = next(obj for obj in objects if obj["type"] == "marking-definition")
    indicator = next(obj for obj in objects if obj["type"] == "indicator")
    assert marking["id"] == TLP_SPEC_IDS["white"]
    assert marking["definition_type"] == "tlp"
    assert marking["definition"] == {"tlp": "white"}
    assert indicator["object_marking_refs"] == [TLP_SPEC_IDS["white"]]


def _malware_bundle_missing_is_family(bundle_id: str) -> dict:
    """Two of these fail identically; only their bundle id tells them apart."""
    return {
        "type": "bundle",
        "id": bundle_id,
        "objects": [
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": f"malware--{uuid.uuid4()}",
                "name": "BadMalware",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
                # missing required 'is_family'
            }
        ],
    }


@pytest.mark.asyncio
async def test_stix_bundle_validator_repeat_guard_separates_distinct_bundles():
    """Two different bundles failing the same way keep separate counters."""
    _, executor = build_stix_bundle_validator_tool()

    await _validate(_malware_bundle_missing_is_family(f"bundle--{uuid.uuid4()}"), executor)
    await _validate(_malware_bundle_missing_is_family(f"bundle--{uuid.uuid4()}"), executor)
    third = await _validate(_malware_bundle_missing_is_family(f"bundle--{uuid.uuid4()}"), executor)

    assert third["valid"] is False
    assert third.get("terminal") is not True


@pytest.mark.asyncio
async def test_stix_bundle_validator_repeat_guard_fires_without_a_bundle_id():
    """A generated bundle id must not make every call look like a new bundle."""
    _, executor = build_stix_bundle_validator_tool()

    for _ in range(2):
        await _validate(_malware_bundle_missing_is_family("bundle--broken"), executor)
    third = await _validate(_malware_bundle_missing_is_family("bundle--broken"), executor)

    assert third["terminal"] is True
    assert third["repeat_count"] == 3


@pytest.mark.asyncio
async def test_stix_bundle_validator_reports_the_replaced_definition_body():
    """A body dropped by TLP normalization is named in the warning."""
    bundle = _bundle_with_marking(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": TLP_SPEC_IDS["red"],
            "created": TLP_SPEC_CREATED,
            "definition_type": "tlp",
            "name": "TLP:RED",
            "definition": {"statement": "internal"},
        }
    )

    payload = await _validate(bundle)

    assert payload["valid"] is True, payload["errors"]
    details = " ".join(payload["marking_details"])
    assert "statement" in details and "internal" in details
    assert "{'tlp': 'red'}" in details


# ── Standard ids ────────────────────────────────────────────────────


def _campaign_without_an_id() -> dict:
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            {
                "type": "campaign",
                "spec_version": "2.1",
                "name": "attack",
                "created": "2022-11-25T19:00:05.000Z",
                "modified": "2022-11-25T19:00:05.000Z",
            }
        ],
    }


@pytest.mark.asyncio
async def test_a_repaired_id_is_the_standard_id_pycti_gives(caplog):
    """OpenCTI recognises an object it holds by this id."""
    from pycti import OpenCTIApiClient

    bundle = _campaign_without_an_id()
    expected = OpenCTIApiClient(
        "http://localhost:0", "dummy", perform_health_check=False
    ).stix2.generate_standard_id_from_stix(dict(bundle["objects"][0]))

    payload = await _validate(bundle)

    assert payload["bundle"]["objects"][0]["id"] == expected
    assert "standard id" not in caplog.text


@pytest.mark.asyncio
async def test_without_pycti_a_repaired_id_falls_back_and_says_why_once(monkeypatch, caplog):
    """An image without libmagic cannot import pycti: the id is the
    platform's own, and a warning says so once rather than never."""
    import sys

    from app.services.builtin_tools import stix

    monkeypatch.setitem(sys.modules, "pycti", None)
    monkeypatch.setattr(stix, "_PYCTI_STIX2_HELPER", None)
    monkeypatch.setattr(stix, "_PYCTI_IMPORT_FAILURE", None)

    first = await _validate(_campaign_without_an_id())
    second = await _validate(_campaign_without_an_id())

    repaired = first["bundle"]["objects"][0]["id"]
    assert repaired.startswith("campaign--")
    assert repaired == second["bundle"]["objects"][0]["id"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "standard id" in r.getMessage()]
    assert len(warnings) == 1
    assert "pycti" in warnings[0].getMessage()
    assert "campaign" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_failed_pycti_import_is_not_retried_for_every_object(monkeypatch):
    """The import is heavy and a bundle repairs its objects one by one: once it
    failed, the process falls back without trying again."""
    import sys

    from app.services.builtin_tools import stix

    pycti = sys.modules.get("pycti")
    monkeypatch.setattr(stix, "_PYCTI_STIX2_HELPER", None)
    monkeypatch.setattr(stix, "_PYCTI_IMPORT_FAILURE", None)
    monkeypatch.setitem(sys.modules, "pycti", None)
    fallback = (await _validate(_campaign_without_an_id()))["bundle"]["objects"][0]["id"]

    # pycti importable again: a retried import would now give the standard id.
    if pycti is None:
        monkeypatch.delitem(sys.modules, "pycti")
    else:
        monkeypatch.setitem(sys.modules, "pycti", pycti)
    again = (await _validate(_campaign_without_an_id()))["bundle"]["objects"][0]["id"]

    assert again == fallback


@pytest.mark.asyncio
async def test_each_cause_of_a_fallback_is_said_once(monkeypatch, caplog):
    """Two failures of one exception class with different reasons are two notices;
    the same reason again is not."""
    from app.services.builtin_tools import stix

    class _Helper:
        def generate_standard_id_from_stix(self, data):
            raise ValueError(f"no identifying rule for {data['type']}")

    monkeypatch.setattr(stix, "_PYCTI_STIX2_HELPER", _Helper())
    bundle = _campaign_without_an_id()
    bundle["objects"].append(dict(bundle["objects"][0], type="intrusion-set"))
    bundle["objects"].append(dict(bundle["objects"][0], name="again"))

    await _validate(bundle)

    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "standard id" in r.getMessage()]
    assert len(said) == 2
    assert any("no identifying rule for campaign" in m for m in said)
    assert any("no identifying rule for intrusion-set" in m for m in said)
