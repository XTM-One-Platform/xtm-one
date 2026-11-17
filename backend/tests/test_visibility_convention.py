"""The one visibility convention of the list endpoints (app/services/visibility.py)."""

from __future__ import annotations

import asyncio
import importlib
import inspect

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.services.visibility import (
    DEPRECATED_VISIBILITY_PARAMS,
    VISIBILITY_PARAM,
    VISIBILITY_VALUES,
    SegmentCounts,
    resolve_visibility,
    segment_counts,
    to_visibility,
)


def test_segments_are_in_display_order():
    assert VISIBILITY_VALUES == ("mine", "shared", "company", "all")
    assert VISIBILITY_PARAM == "vis"
    assert DEPRECATED_VISIBILITY_PARAMS == ("visibility", "ownership", "scope")


@pytest.mark.parametrize("value", ["mine", "shared", "company", "all"])
def test_every_segment_reads_as_itself(value):
    assert to_visibility(value) == value
    assert to_visibility(f" {value.upper()} ") == value


@pytest.mark.parametrize(("alias", "segment"), [("company_managed", "company"), ("own", "mine"), ("everyone", "all")])
def test_deprecated_values_map_onto_the_segments(alias, segment):
    assert to_visibility(alias) == segment


@pytest.mark.parametrize("value", [None, "", "  "])
def test_an_empty_value_is_an_omitted_one(value):
    assert to_visibility(value) is None


def test_an_unknown_value_is_a_422_never_a_dropped_filter():
    with pytest.raises(HTTPException) as caught:
        to_visibility("min")
    assert caught.value.status_code == 422
    assert "mine, shared, company, all" in caught.value.detail


def test_vis_wins_over_the_deprecated_aliases():
    assert resolve_visibility("company", "mine") == "company"
    assert resolve_visibility(None, "shared") == "shared"
    assert resolve_visibility("", None, "company_managed") == "company"


def test_the_endpoint_default_stays_all():
    assert resolve_visibility(None, None) == "all"
    assert resolve_visibility(None, default="mine") == "mine"


@pytest.mark.parametrize(
    ("module", "endpoint"),
    [("app.api.v1.presentations", "list_presentations"), ("app.api.v1.video_studio", "list_projects")],
)
def test_the_library_endpoints_take_vis_and_keep_the_deprecated_alias(module, endpoint):
    params = inspect.signature(getattr(importlib.import_module(module), endpoint)).parameters
    assert "vis" in params
    assert "visibility" in params
    assert params["visibility"].default.deprecated is True


def test_segment_counts_asks_each_segment_once_and_derives_none():
    """Owned, shared and company rows -> the four counts, each one asked, never summed."""
    rows = [("mine", 1), ("mine", 2), ("shared", 3), ("company", 4), ("company", 5), ("company", 6)]
    asked: list[str] = []

    async def count(segment):
        asked.append(segment)
        return len(rows) if segment == "all" else sum(1 for owner, _ in rows if owner == segment)

    counts = asyncio.run(segment_counts(count))

    assert counts == SegmentCounts(mine=2, shared=1, company=3, all=6)
    assert asked == list(VISIBILITY_VALUES)
    assert counts.mine + counts.shared + counts.company == counts.all


#: Every paginated list of the visibility control, by router module: (list path, its segment-counts path).
SEGMENT_COUNT_ROUTES = [
    ("app.api.v1.presentations", "", "/segment-counts"),
    ("app.api.v1.video_studio", "/projects", "/projects/segment-counts"),
    ("app.api.v1.files", "", "/segment-counts"),
    ("app.api.v1.chat", "/sub-agent-tasks", "/sub-agent-tasks/segment-counts"),
    # The run feed's filters are its count's (``/runs/count`` also narrows to one assignment).
    ("app.api.v1.assignments", "/runs/count", "/runs/segment-counts"),
    ("app.api.v1.flow_runs", "/runs/all", "/runs/segment-counts"),
    ("app.api.v1.work_items", "", "/segment-counts"),
    ("app.api.v1.shared_state", "", "/segment-counts"),
    ("app.api.v1.worksets", "", "/segment-counts"),
]


@pytest.mark.parametrize(("module", "list_path", "counts_path"), SEGMENT_COUNT_ROUTES)
def test_every_paginated_list_answers_its_segment_counts(module, list_path, counts_path):
    """One pattern on every list: ``GET <list>/segment-counts``, answering ``SegmentCounts``,
    taking the list's filters but never the segment, declared before any ``/{id}`` route
    that would swallow the literal."""
    routes = [r for r in importlib.import_module(module).router.routes if isinstance(r, APIRoute)]
    gets = [r for r in routes if "GET" in r.methods]
    paths = [r.path for r in gets]
    assert list_path in paths, list_path
    assert counts_path in paths, counts_path
    counts_route = gets[paths.index(counts_path)]
    assert counts_route.response_model is SegmentCounts
    taken = {p.alias or p.name for p in counts_route.dependant.query_params}
    assert not taken & {VISIBILITY_PARAM, *DEPRECATED_VISIBILITY_PARAMS} - {"scope"}
    list_taken = {p.alias or p.name for p in gets[paths.index(list_path)].dependant.query_params}
    assert taken <= list_taken, taken - list_taken
    index = paths.index(counts_path)
    prefix = counts_path.rsplit("/", 1)[0]
    for earlier in paths[:index]:
        assert not (earlier.startswith(prefix + "/{") and earlier.count("/") == counts_path.count("/")), earlier
