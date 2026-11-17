import json
import uuid

import pytest

from app.services.builtin_tools import build_timeline_diff_tool
from app.tools.base import ToolCall


def _make_bundle(*objects):
    return {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": list(objects),
    }


def _entity(
    stix_id=None,
    created="2024-01-01T00:00:00.000Z",
    modified="2024-01-15T00:00:00.000Z",
    first_seen=None,
    last_seen=None,
    published=None,
    name="entity",
):
    obj = {
        "type": "campaign",
        "spec_version": "2.1",
        "id": stix_id or f"campaign--{uuid.uuid4()}",
        "name": name,
        "created": created,
        "modified": modified,
    }
    if first_seen is not None:
        obj["first_seen"] = first_seen
    if last_seen is not None:
        obj["last_seen"] = last_seen
    if published is not None:
        obj["published"] = published
    return obj


async def _run(bundle, comparison=None):
    _, executor = build_timeline_diff_tool()
    args = {"bundle": bundle}
    if comparison is not None:
        args["comparison_bundle"] = comparison
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="analyze_stix_timeline",
            arguments=args,
        )
    )
    assert not result.is_error
    return json.loads(result.content)


@pytest.mark.asyncio
async def test_single_bundle_timestamp_coverage():
    obj = _entity(
        first_seen="2024-01-05T00:00:00Z",
        last_seen="2024-02-10T00:00:00Z",
    )
    bundle = _make_bundle(obj)

    payload = await _run(bundle)

    assert payload["inputs"]["object_count"] == 1
    assert payload["results"]["timestamp_coverage_count"] == 1
    coverage = payload["results"]["timestamp_coverage"][0]
    assert "created" in coverage["present_fields"]
    assert "first_seen" in coverage["present_fields"]
    assert "published" in coverage["missing_fields"]


@pytest.mark.asyncio
async def test_single_bundle_windowed_trends():
    objects = [
        _entity(name=f"e{i}", created=f"2024-01-{i + 1:02d}T00:00:00Z")
        for i in range(10)
    ]
    bundle = _make_bundle(*objects)

    payload = await _run(bundle)

    created_trend = payload["results"]["trends"]["created"]
    assert created_trend["total_objects_with_field"] == 10
    assert len(created_trend["daily_counts"]) == 10


@pytest.mark.asyncio
async def test_dual_bundle_detects_shifted_timestamps():
    cid = f"campaign--{uuid.uuid4()}"
    obj_a = _entity(stix_id=cid, modified="2024-01-15T00:00:00Z")
    obj_b = _entity(stix_id=cid, modified="2024-02-15T00:00:00Z")

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    assert payload["intermediate_values"]["objects_with_drifts"] == 1
    diff = payload["results"]["per_object_diffs"][0]
    ts_diff = next(d for d in diff["timestamp_diffs"] if d["field"] == "modified")
    assert ts_diff["status"] == "shifted"
    assert ts_diff["direction"] == "forward"


@pytest.mark.asyncio
async def test_dual_bundle_detects_backfilled_timestamps():
    cid = f"campaign--{uuid.uuid4()}"
    obj_a = _entity(stix_id=cid)
    obj_b = _entity(stix_id=cid, first_seen="2024-01-05T00:00:00Z")

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    assert payload["intermediate_values"]["objects_with_drifts"] == 1
    diff = payload["results"]["per_object_diffs"][0]
    backfilled = [d for d in diff["timestamp_diffs"] if d["status"] == "backfilled"]
    assert len(backfilled) >= 1
    assert backfilled[0]["field"] == "first_seen"


@pytest.mark.asyncio
async def test_missing_dates_handled_gracefully():
    obj = _entity()  # no first_seen, last_seen, published
    bundle = _make_bundle(obj)

    payload = await _run(bundle)

    # Should not error, should report missing fields
    coverage = payload["results"]["timestamp_coverage"][0]
    assert "first_seen" in coverage["missing_fields"]
    assert "last_seen" in coverage["missing_fields"]
    assert "published" in coverage["missing_fields"]


@pytest.mark.asyncio
async def test_timezone_differences():
    cid = f"campaign--{uuid.uuid4()}"
    obj_a = _entity(stix_id=cid, modified="2024-01-15T00:00:00Z")
    # Same instant, different timezone representation
    obj_b = _entity(stix_id=cid, modified="2024-01-15T05:00:00+05:00")

    bundle_a = _make_bundle(obj_a)
    bundle_b = _make_bundle(obj_b)

    payload = await _run(bundle_a, bundle_b)

    # Same absolute time, so no drift should be detected for modified
    drifts = payload["results"]["per_object_diffs"]
    modified_drifts = []
    for d in drifts:
        for td in d["timestamp_diffs"]:
            if td["field"] == "modified":
                modified_drifts.append(td)
    assert len(modified_drifts) == 0


@pytest.mark.asyncio
async def test_empty_bundle():
    bundle = _make_bundle()
    payload = await _run(bundle)
    assert payload["inputs"]["object_count"] == 0


@pytest.mark.asyncio
async def test_json_string_input():
    obj = _entity()
    bundle = _make_bundle(obj)

    _, executor = build_timeline_diff_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="analyze_stix_timeline",
            arguments={"bundle_json": json.dumps(bundle)},
        )
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["inputs"]["object_count"] == 1


@pytest.mark.asyncio
async def test_no_bundle_returns_error():
    _, executor = build_timeline_diff_tool()
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="analyze_stix_timeline",
            arguments={},
        )
    )
    assert result.is_error
    payload = json.loads(result.content)
    assert payload.get("error") is True
