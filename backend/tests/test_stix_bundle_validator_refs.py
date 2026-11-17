"""stix_bundle_validator: refs to absent objects and losses against the input (#11800)."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.services.builtin_tools.stix import stix_validator_strict_refs
from app.tools.base import ToolCall

_TS = "2022-11-25T19:00:05.000Z"
_TLP_GREEN = "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da"


async def _validate(arguments: dict, *, strict_refs: bool = False) -> dict:
    _, executor = build_stix_bundle_validator_tool(strict_refs=strict_refs)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="stix_bundle_validator", arguments=arguments)
    )
    assert not result.is_error
    return json.loads(result.content)


def _campaign(campaign_id: str, **extra) -> dict:
    return {
        "type": "campaign",
        "spec_version": "2.1",
        "id": campaign_id,
        "name": "attack",
        "created": _TS,
        "modified": _TS,
        **extra,
    }


def _report_bundle(object_refs: list[str], campaign_id: str) -> dict:
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            _campaign(campaign_id),
            {
                "type": "report",
                "spec_version": "2.1",
                "id": f"report--{uuid.uuid4()}",
                "name": "weekly",
                "published": _TS,
                "created": _TS,
                "modified": _TS,
                "object_refs": object_refs,
                "object_marking_refs": [_TLP_GREEN],
            },
        ],
    }


@pytest.mark.asyncio
async def test_strict_mode_refuses_a_report_ref_absent_from_the_bundle():
    campaign_id = f"campaign--{uuid.uuid4()}"
    missing = f"malware--{uuid.uuid4()}"
    payload = await _validate({"bundle": _report_bundle([campaign_id, missing], campaign_id)}, strict_refs=True)

    assert payload["valid"] is False
    assert payload["error_count"] == 1
    (error,) = payload["errors"]
    assert missing in error
    assert "object_refs[1]" in error
    assert "objects[1] (report)" in error


@pytest.mark.asyncio
async def test_default_mode_warns_on_a_ref_absent_from_the_bundle():
    campaign_id = f"campaign--{uuid.uuid4()}"
    missing = f"malware--{uuid.uuid4()}"
    payload = await _validate({"bundle": _report_bundle([campaign_id, missing], campaign_id)})

    assert payload["valid"] is True, payload["errors"]
    assert payload["error_count"] == 0
    assert payload["warning_count"] == 1
    assert missing in payload["warnings"][0]


@pytest.mark.asyncio
async def test_a_complete_bundle_has_no_finding_in_strict_mode():
    """Resolved refs and a spec TLP marking without its definition stay valid."""
    campaign_id = f"campaign--{uuid.uuid4()}"
    payload = await _validate({"bundle": _report_bundle([campaign_id], campaign_id)}, strict_refs=True)

    assert payload["valid"] is True, payload["errors"]
    assert "warnings" not in payload


@pytest.mark.asyncio
async def test_relationship_ends_are_checked():
    campaign_id = f"campaign--{uuid.uuid4()}"
    missing = f"malware--{uuid.uuid4()}"
    bundle = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [
            _campaign(campaign_id),
            {
                "type": "relationship",
                "spec_version": "2.1",
                "id": f"relationship--{uuid.uuid4()}",
                "relationship_type": "uses",
                "source_ref": campaign_id,
                "target_ref": missing,
                "created": _TS,
                "modified": _TS,
            },
        ],
    }
    payload = await _validate({"bundle": bundle}, strict_refs=True)

    assert payload["valid"] is False
    assert len(payload["errors"]) == 1
    assert "target_ref" in payload["errors"][0] and missing in payload["errors"][0]


@pytest.mark.asyncio
async def test_many_dangling_refs_are_listed_with_a_cap():
    campaign_id = f"campaign--{uuid.uuid4()}"
    refs = [campaign_id] + [f"malware--{uuid.uuid4()}" for _ in range(60)]
    payload = await _validate({"bundle": _report_bundle(refs, campaign_id)}, strict_refs=True)

    assert payload["valid"] is False
    assert len(payload["errors"]) == 51
    assert payload["errors"][-1] == "... and 10 more unresolved refs."


@pytest.mark.asyncio
async def test_input_bundle_reports_a_dropped_property():
    campaign_id = f"campaign--{uuid.uuid4()}"
    original = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": [_campaign(campaign_id, x_opencti_content="<p>body</p>")],
    }
    candidate = {
        "type": "bundle",
        "id": original["id"],
        "objects": [_campaign(campaign_id)],
    }
    payload = await _validate({"bundle": candidate, "input_bundle": original})

    assert payload["valid"] is True, payload["errors"]
    assert payload["warning_count"] == 1
    assert "x_opencti_content" in payload["warnings"][0]
    assert campaign_id in payload["warnings"][0]


@pytest.mark.asyncio
async def test_input_bundle_reports_a_dropped_object_and_accepts_a_json_string():
    kept = f"campaign--{uuid.uuid4()}"
    dropped = f"campaign--{uuid.uuid4()}"
    original = {"type": "bundle", "id": f"bundle--{uuid.uuid4()}", "objects": [_campaign(kept), _campaign(dropped)]}
    candidate = {"type": "bundle", "id": original["id"], "objects": [_campaign(kept)]}
    payload = await _validate({"bundle": candidate, "input_bundle": json.dumps(original)})

    assert payload["valid"] is True, payload["errors"]
    assert payload["warnings"] == [f"input object '{dropped}' (campaign) is not in the bundle."]


_INHERITED = "malware--1f8e3a6c-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_strict_mode_warns_on_a_ref_already_dangling_in_the_input():
    campaign_id = f"campaign--{uuid.uuid4()}"
    original = _report_bundle([campaign_id, _INHERITED], campaign_id)
    patch = [{"op": "add", "path": "/objects/1/description", "value": "enriched"}]

    for arguments in (
        {"bundle": json.loads(json.dumps(original)), "input_bundle": original},
        {"patch": patch, "input_bundle": original},
        {"patch": patch, "input_bundle": json.dumps(original)},
    ):
        payload = await _validate(arguments, strict_refs=True)

        assert payload["valid"] is True, payload["errors"]
        assert payload["error_count"] == 0
        assert payload["warning_count"] == 1
        (warning,) = payload["warnings"]
        assert _INHERITED in warning
        assert "already dangling in input_bundle" in warning


@pytest.mark.asyncio
async def test_strict_mode_refuses_a_dangling_ref_the_agent_introduced():
    campaign_id = f"campaign--{uuid.uuid4()}"
    original = _report_bundle([campaign_id, _INHERITED], campaign_id)
    introduced = f"tool--{uuid.uuid4()}"
    # Inserted first: the inherited ref moves to another index and stays a warning.
    patch = [{"op": "add", "path": "/objects/1/object_refs/0", "value": introduced}]
    payload = await _validate({"patch": patch, "input_bundle": original}, strict_refs=True)

    assert payload["valid"] is False
    assert payload["error_count"] == 1
    (error,) = payload["errors"]
    assert introduced in error
    assert _INHERITED not in error
    # The changed object_refs list is also reported against the input.
    ref_warnings = [w for w in payload["warnings"] if "is not an object of this bundle" in w]
    assert len(ref_warnings) == 1
    assert _INHERITED in ref_warnings[0]
    assert "already dangling in input_bundle" in ref_warnings[0]


@pytest.mark.asyncio
async def test_strict_mode_without_input_still_refuses_an_inherited_ref():
    campaign_id = f"campaign--{uuid.uuid4()}"
    payload = await _validate({"bundle": _report_bundle([campaign_id, _INHERITED], campaign_id)}, strict_refs=True)

    assert payload["valid"] is False
    assert payload["error_count"] == 1
    assert _INHERITED in payload["errors"][0]
    assert "input_bundle" not in payload["errors"][0]


@pytest.mark.asyncio
async def test_default_mode_with_input_keeps_every_dangling_ref_a_warning():
    campaign_id = f"campaign--{uuid.uuid4()}"
    original = _report_bundle([campaign_id, _INHERITED], campaign_id)
    introduced = f"tool--{uuid.uuid4()}"
    patch = [{"op": "add", "path": "/objects/1/object_refs/-", "value": introduced}]
    payload = await _validate({"patch": patch, "input_bundle": original})

    assert payload["valid"] is True, payload["errors"]
    ref_warnings = [w for w in payload["warnings"] if "is not an object of this bundle" in w]
    assert len(ref_warnings) == 2
    assert any(introduced in warning for warning in ref_warnings)
    assert any(_INHERITED in warning for warning in ref_warnings)
    # Non-strict mode keeps today's messages: no input split is reported.
    assert not any("already dangling" in warning for warning in ref_warnings)


def test_strict_refs_follows_the_finalize_configuration():
    fast_path = SimpleNamespace(
        config={"finalize_on_builtin_tool": {"tool_name": "stix_bundle_validator"}},
        finalize_tool_ref=None,
    )
    pipeline = SimpleNamespace(config={}, finalize_tool_ref={"source": "builtin", "tool_name": "stix_bundle_validator"})
    plain = SimpleNamespace(config={}, finalize_tool_ref=None)
    other = SimpleNamespace(config=None, finalize_tool_ref={"source": "integration", "tool_name": "x"})

    assert stix_validator_strict_refs(fast_path) is True
    assert stix_validator_strict_refs(pipeline) is True
    assert stix_validator_strict_refs(plain) is False
    assert stix_validator_strict_refs(other) is False
