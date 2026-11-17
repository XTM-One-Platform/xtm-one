"""stix_bundle_validator: spec defects the input already carries stay warnings."""

import json
import uuid
from typing import Any

import pytest

from app.services.builtin_tools import build_stix_bundle_validator_tool
from app.tools.base import ToolCall

_TS = "2022-11-25T19:00:05.000Z"


async def _validate(arguments: dict[str, Any], *, strict_refs: bool = True) -> dict[str, Any]:
    _, executor = build_stix_bundle_validator_tool(strict_refs=strict_refs)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="stix_bundle_validator", arguments=arguments)
    )
    assert not result.is_error
    payload: dict[str, Any] = json.loads(result.content)
    return payload


def _empty_report(report_id: str) -> dict[str, Any]:
    return {
        "type": "report",
        "spec_version": "2.1",
        "id": report_id,
        "name": "weekly",
        "published": _TS,
        "created": _TS,
        "modified": _TS,
        "report_types": ["threat-report"],
        "object_refs": [],
    }


def _input_with_empty_report(report_id: str) -> dict[str, Any]:
    return {"type": "bundle", "id": f"bundle--{uuid.uuid4()}", "objects": [_empty_report(report_id)]}


def _note(report_id: str) -> dict[str, Any]:
    return {
        "type": "note",
        "spec_version": "2.1",
        "id": f"note--{uuid.uuid4()}",
        "created": _TS,
        "modified": _TS,
        "content": "analyst comment",
        "object_refs": [report_id],
    }


@pytest.mark.asyncio
async def test_a_spec_defect_already_in_the_input_is_a_warning() -> None:
    report_id = f"report--{uuid.uuid4()}"
    note = _note(report_id)
    payload = await _validate(
        {
            "input_bundle": _input_with_empty_report(report_id),
            "patch": [{"op": "add", "path": "/objects/-", "value": note}],
        }
    )

    assert payload["valid"] is True, payload["errors"]
    spec_warnings = [w for w in payload["warnings"] if "stix2 validation error" in w]
    assert len(spec_warnings) == 1
    assert report_id in spec_warnings[0]
    assert "already present in input_bundle" in spec_warnings[0]
    assert note["id"] in {obj["id"] for obj in payload["bundle"]["objects"]}


@pytest.mark.asyncio
async def test_the_same_defect_on_an_added_object_is_an_error() -> None:
    report_id = f"report--{uuid.uuid4()}"
    added_id = f"report--{uuid.uuid4()}"
    payload = await _validate(
        {
            "input_bundle": _input_with_empty_report(report_id),
            "patch": [{"op": "add", "path": "/objects/-", "value": _empty_report(added_id)}],
        }
    )

    assert payload["valid"] is False
    assert payload["error_count"] == 1
    (error,) = payload["errors"]
    assert added_id in error
    assert report_id not in error
    assert any(report_id in w and "already present in input_bundle" in w for w in payload["warnings"])


@pytest.mark.asyncio
async def test_a_defect_the_patch_introduces_on_an_input_object_is_an_error() -> None:
    report_id = f"report--{uuid.uuid4()}"
    payload = await _validate(
        {"input_bundle": _input_with_empty_report(report_id), "patch": [{"op": "remove", "path": "/objects/0/name"}]}
    )

    assert payload["valid"] is False
    assert any(report_id in error and "name" in error for error in payload["errors"])
    assert not any("already present in input_bundle" in w for w in payload.get("warnings", []))


@pytest.mark.asyncio
async def test_without_input_the_defect_stays_an_error() -> None:
    report_id = f"report--{uuid.uuid4()}"
    payload = await _validate({"bundle": _input_with_empty_report(report_id)})

    assert payload["valid"] is False
    assert payload["error_count"] == 1
    assert payload["errors"][0].startswith("stix2 validation error:")
    assert "input_bundle" not in payload["errors"][0]
