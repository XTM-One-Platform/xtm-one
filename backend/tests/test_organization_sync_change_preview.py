# SPDX-License-Identifier: Apache-2.0

"""The preview of a sync's filter change, before it is saved (#1982 S2)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import organization_context_import as imp
from app.services import organization_context_outbound as outbound
from app.services import organization_context_sync as osync

#: The preview reads no row itself: every read is the stubbed planner's.
NO_DB: Any = cast(Any, None)


def _sync(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "section": "assets",
        "source": "openaev",
        "direction": "inbound",
        "filters": {"tags": ["old"]},
        "outbound_ids": {},
        "excluded_external_ids": [],
    }
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.asyncio
async def test_inbound_preview_plans_with_the_new_filters_and_splits_removed_from_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def preview_import(_db: Any, **kwargs: Any) -> tuple[imp.ImportPlan, int]:
        seen.update(kwargs)
        plan = imp.ImportPlan(
            section="assets",
            source="openaev",
            source_total=3,
            read=3,
            new=["web-1"],
            updated=["db-1"],
            gone=["mine-1", "theirs-1"],
            gone_kept=["theirs-1"],
            excluded_removed=["excl-1"],
            remove_gone=kwargs["remove_gone"],
        )
        return plan, 7

    monkeypatch.setattr(imp, "preview_import", preview_import)
    sync = _sync()
    user = uuid.uuid4()
    change = await osync.preview_change(
        NO_DB,
        sync,
        user_id=user,
        filters={"tags": ["new"]},
        batch_type="",
        remove_gone=True,
        excluded_ids=[" excl ", "excl", ""],
    )
    # Planned with the proposed settings and this administrator's token, as this sync.
    assert seen["filters"] == {"tags": ["new"]} and seen["user_id"] == user
    assert seen["sync_id"] == str(sync.id) and seen["excluded_ids"] == ["excl"]
    assert change["counts"] == {"enter": 1, "update": 1, "leave": 2, "kept": 1}
    assert change["leave"] == ["mine-1", "excl-1"] and change["kept"] == ["theirs-1"]
    # The stored sync is not changed by a preview.
    assert sync.filters == {"tags": ["old"]}


@pytest.mark.asyncio
async def test_inbound_preview_removes_nothing_that_left_the_filters_when_removal_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def preview_import(_db: Any, **kwargs: Any) -> tuple[imp.ImportPlan, int]:
        return imp.ImportPlan(section="assets", source="openaev", source_total=0, read=0, gone=["a"]), 1

    monkeypatch.setattr(imp, "preview_import", preview_import)
    change = await osync.preview_change(
        NO_DB, _sync(), user_id=uuid.uuid4(), filters={}, batch_type="", remove_gone=False, excluded_ids=None
    )
    assert change["counts"]["leave"] == 0 and change["error"] is None


@pytest.mark.asyncio
async def test_outbound_preview_plans_without_writing_and_keeps_the_created_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def run_outbound(
        _db: Any, sync: Any, *, user_id: uuid.UUID, apply: bool
    ) -> tuple[outbound.OutboundPlan, dict[str, str]]:
        seen["apply"], seen["filters"], seen["ids"] = apply, sync.filters, sync.outbound_ids
        sync.outbound_ids.pop("dmz", None)  # a planner that edits its copy never reaches the stored sync
        return (
            outbound.OutboundPlan(
                platform="openaev",
                create=[("Web", "high")],
                update=[("g1", "DMZ", "high")],
                delete=[("lab", "g2")],
            ),
            {},
        )

    monkeypatch.setattr(outbound, "run_outbound", run_outbound)
    sync = _sync(section="asset_groups", direction="outbound", outbound_ids={"dmz": "g1", "lab": "g2"})
    change = await osync.preview_change(
        NO_DB,
        sync,
        user_id=uuid.uuid4(),
        filters={"tags": ["prod"]},
        batch_type="",
        remove_gone=True,
        excluded_ids=None,
    )
    assert seen == {"apply": False, "filters": {"tags": ["prod"]}, "ids": {"lab": "g2"}}
    assert change["direction"] == "outbound"
    assert change["counts"] == {"enter": 1, "update": 1, "leave": 1, "kept": 0}
    assert change["leave"] == ["lab"]
    assert sync.outbound_ids == {"dmz": "g1", "lab": "g2"} and sync.filters == {"tags": ["old"]}
