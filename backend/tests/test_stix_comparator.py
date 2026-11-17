import json
import uuid

import pytest

from app.services.builtin_tools import build_stix_comparator_tool
from app.tools.base import ToolCall


def _make_bundle(*objects):
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": list(objects),
    }


def _campaign(name="attack", confidence=80, stix_id=None):
    return {
        "type": "campaign",
        "spec_version": "2.1",
        "id": stix_id or f"campaign--{uuid.uuid4()}",
        "name": name,
        "confidence": confidence,
        "created": "2024-01-01T00:00:00.000Z",
        "modified": "2024-01-01T00:00:00.000Z",
    }


def _relationship(src_ref, tgt_ref, rel_type="uses", stix_id=None):
    return {
        "type": "relationship",
        "spec_version": "2.1",
        "id": stix_id or f"relationship--{uuid.uuid4()}",
        "relationship_type": rel_type,
        "source_ref": src_ref,
        "target_ref": tgt_ref,
        "created": "2024-01-01T00:00:00.000Z",
        "modified": "2024-01-01T00:00:00.000Z",
    }


async def _run(bundle_a, bundle_b):
    _, executor = build_stix_comparator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="compare_stix_bundles",
            arguments={"bundle_a": bundle_a, "bundle_b": bundle_b},
        )
    )
    assert not result.is_error
    return json.loads(result.content)


@pytest.mark.asyncio
async def test_identical_bundles_produce_zero_diffs():
    cid = f"campaign--{uuid.uuid4()}"
    obj = _campaign(stix_id=cid)
    bundle = _make_bundle(obj)
    payload = await _run(bundle, bundle)

    assert payload["results"]["objects"]["added_count"] == 0
    assert payload["results"]["objects"]["removed_count"] == 0
    assert payload["results"]["objects"]["modified_count"] == 0
    assert payload["results"]["relationships"]["added_count"] == 0
    assert payload["results"]["duplicate_count"] == 0


@pytest.mark.asyncio
async def test_added_and_removed_objects():
    cid = f"campaign--{uuid.uuid4()}"
    mid = f"malware--{uuid.uuid4()}"
    obj_a = _campaign(stix_id=cid)
    obj_b = _campaign(name="other", stix_id=mid)
    obj_b["type"] = "malware"
    obj_b["is_family"] = False

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    assert payload["results"]["objects"]["removed_count"] == 1
    assert payload["results"]["objects"]["added_count"] == 1
    assert payload["results"]["objects"]["removed"][0]["id"] == cid
    assert payload["results"]["objects"]["added"][0]["id"] == mid


@pytest.mark.asyncio
async def test_modified_object_field_diffs():
    cid = f"campaign--{uuid.uuid4()}"
    obj_a = _campaign(stix_id=cid, confidence=80)
    obj_b = _campaign(stix_id=cid, confidence=40)
    obj_b["labels"] = ["new-label"]

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    assert payload["results"]["objects"]["modified_count"] == 1
    modified = payload["results"]["objects"]["modified"][0]
    assert modified["id"] == cid
    field_names = {c["field"] for c in modified["changes"]}
    assert "confidence" in field_names
    assert "labels" in field_names


@pytest.mark.asyncio
async def test_relationship_diffs():
    cid = f"campaign--{uuid.uuid4()}"
    mid = f"malware--{uuid.uuid4()}"
    rid = f"relationship--{uuid.uuid4()}"

    obj = _campaign(stix_id=cid)
    rel = _relationship(cid, mid, stix_id=rid)

    bundle_a = _make_bundle(obj)
    bundle_b = _make_bundle(obj, rel)

    payload = await _run(bundle_a, bundle_b)

    assert payload["results"]["relationships"]["added_count"] == 1
    assert payload["results"]["relationships"]["added"][0]["id"] == rid


@pytest.mark.asyncio
async def test_duplicate_detection():
    cid_a = f"campaign--{uuid.uuid4()}"
    cid_b = f"campaign--{uuid.uuid4()}"
    obj_a = _campaign(name="Operation X", stix_id=cid_a)
    obj_b = _campaign(name="Operation X", stix_id=cid_b)

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    assert payload["results"]["duplicate_count"] >= 1
    dup = payload["results"]["duplicates"][0]
    assert dup["name"] == "Operation X"
    assert dup["type"] == "campaign"


@pytest.mark.asyncio
async def test_empty_bundles():
    bundle_a = _make_bundle()
    bundle_b = _make_bundle()

    payload = await _run(bundle_a, bundle_b)

    assert payload["inputs"]["bundle_a_object_count"] == 0
    assert payload["results"]["objects"]["added_count"] == 0
    assert payload["results"]["duplicate_count"] == 0


@pytest.mark.asyncio
async def test_missing_objects_key():
    bundle_a = {"type": "bundle", "id": f"bundle--{uuid.uuid4()}"}
    bundle_b = _make_bundle(_campaign())

    payload = await _run(bundle_a, bundle_b)

    assert payload["inputs"]["bundle_a_object_count"] == 0
    assert payload["results"]["objects"]["added_count"] == 1


@pytest.mark.asyncio
async def test_malformed_bundle_returns_error():
    _, executor = build_stix_comparator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="compare_stix_bundles",
            arguments={},
        )
    )
    assert result.is_error
    payload = json.loads(result.content)
    assert payload.get("error") is True


@pytest.mark.asyncio
async def test_json_string_input():
    cid = f"campaign--{uuid.uuid4()}"
    obj = _campaign(stix_id=cid)
    bundle = _make_bundle(obj)

    _, executor = build_stix_comparator_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="compare_stix_bundles",
            arguments={
                "bundle_a_json": json.dumps(bundle),
                "bundle_b_json": json.dumps(bundle),
            },
        )
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["results"]["objects"]["added_count"] == 0
