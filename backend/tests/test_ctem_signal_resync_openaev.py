# SPDX-License-Identifier: Apache-2.0

"""#2641, #2643: the purge resync replays the open OpenAEV findings, as live intake records them."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signal_resync as resync

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _finding(n: int) -> dict[str, Any]:
    return {
        "finding_id": f"f-{n}",
        "finding_type": "cve",
        "finding_value": f"CVE-2026-000{n}",
        "finding_assets": [{"asset_id": f"a-{n}", "asset_name": f"host-{n}"}],
    }


def _sources(monkeypatch: pytest.MonkeyPatch, integration_ids: list[str], *, excluded: bool = False) -> None:
    async def _enabled(db: Any, provider: str, kind: str = "") -> list[Any]:
        assert (provider, kind) == ("openaev", "listener")
        return [SimpleNamespace(integration_id=i) for i in integration_ids]

    async def _excluded(sessions: Any) -> bool:
        return excluded

    monkeypatch.setattr(resync, "_enabled_sources", _enabled)
    monkeypatch.setattr(resync, "_openaev_findings_excluded", _excluded)


def test_a_search_row_becomes_the_provisioned_finding_created_delivery() -> None:
    payload = resync.finding_delivery(_finding(1), "https://aev.example/")
    assert payload is not None
    found = listeners.match("openaev", "finding_created", payload)
    assert found is not None
    row, fields = found
    # Path 5, outside lens, the finding_created source: the live trigger's row.
    assert (row.trigger_path, row.lens, row.source) == ("new_exposure", "outside", "finding_created")
    # Keyed on the OpenAEV finding id, as the live delivery, so a second resync adds nothing.
    assert fields["external_id"] == "f-1" and fields["ref"] == "openaev finding f-1"
    assert fields["label"] == "CVE CVE-2026-0001 on host-1"
    assert fields["evidence"]["platform_url"] == "https://aev.example"


def test_a_row_without_an_id_is_left_out() -> None:
    assert resync.finding_delivery({"finding_type": "cve"}, "") is None


@pytest.mark.asyncio
async def test_three_findings_over_two_pages_record_three_new_exposures(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2643 acceptance: a fake OpenAEV returning 3 findings over 2 pages records 3 path 5 signals."""
    _sources(monkeypatch, ["11111111-1111-1111-1111-111111111111"])
    pages = {0: [_finding(1), _finding(2)], 1: [_finding(3)]}
    asked: list[dict[str, Any]] = []

    async def _connect(integration_id: str) -> Any:
        return "https://aev.example", {"Authorization": "Bearer t"}

    async def _api(base: str, headers: Any, method: str, path: str, action: str, **kw: Any) -> Any:
        assert (method, path) == ("post", "/api/findings/search")
        asked.append(kw["json"])
        return _Resp({"content": pages[kw["json"]["page"]], "totalPages": 2}), None

    recorded: list[tuple[str, str, str]] = []

    async def _note(provider: str, event: str, payload: dict[str, Any], *, verified: bool) -> bool:
        assert verified is True
        found = listeners.match(provider, event, payload)
        assert found is not None
        recorded.append((found[0].trigger_path, found[0].lens, found[1]["external_id"]))
        return True

    out = await resync.resync_openaev_findings(lambda: _Session(), now=NOW, note=_note, connect=_connect, api=_api)
    assert out["read"] == 3 and out["recorded"] == 3 and out["errors"] == []
    assert recorded == [("new_exposure", "outside", f"f-{n}") for n in (1, 2, 3)]
    # Only the findings open at source, and no age limit by default.
    assert [a["page"] for a in asked] == [0, 1]
    assert asked[0]["filterGroup"]["filters"] == [resync._OPEN_FINDING]


@pytest.mark.asyncio
async def test_a_window_reads_the_findings_updated_since(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, ["11111111-1111-1111-1111-111111111111"])
    asked: list[dict[str, Any]] = []

    async def _connect(integration_id: str) -> Any:
        return "https://aev.example", {}

    async def _api(*args: Any, **kw: Any) -> Any:
        asked.append(kw["json"])
        return _Resp({"content": [], "totalPages": 0}), None

    async def _note(*args: Any, **kw: Any) -> bool:
        return True

    windows = resync.validate_windows({"openaev_findings": 7})
    await resync.resync_openaev_findings(
        lambda: _Session(), windows=windows, now=NOW, note=_note, connect=_connect, api=_api
    )
    since = asked[0]["filterGroup"]["filters"][1]
    assert since == {
        "key": "finding_updated_at",
        "mode": "and",
        "operator": "gte",
        "values": ["2026-11-20T16:00:00.000Z"],
    }


@pytest.mark.asyncio
async def test_an_excluded_source_records_nothing_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, ["11111111-1111-1111-1111-111111111111"], excluded=True)

    async def _api(*args: Any, **kw: Any) -> Any:
        raise AssertionError("an excluded source is never read")

    out = await resync.resync_openaev_findings(lambda: _Session(), now=NOW, api=_api)
    assert out["recorded"] == 0 and out["skipped"] == resync.OPENAEV_FINDINGS_OFF


@pytest.mark.asyncio
async def test_no_enabled_listener_source_skips_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, [])
    out = await resync.resync_openaev_findings(lambda: _Session(), now=NOW)
    assert out["recorded"] == 0 and "no OpenAEV listener source" in out["skipped"]


@pytest.mark.asyncio
async def test_a_refused_token_is_one_error_line_and_the_next_source_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"])

    async def _connect(integration_id: str) -> Any:
        return f"https://{integration_id[:1]}.example", {}

    async def _api(base: str, *args: Any, **kw: Any) -> Any:
        if base.startswith("https://1"):
            return None, "OpenAEV refused the request (401 Unauthorized)"
        return _Resp({"content": [_finding(7)], "totalPages": 1}), None

    async def _note(*args: Any, **kw: Any) -> bool:
        return True

    out = await resync.resync_openaev_findings(lambda: _Session(), now=NOW, note=_note, connect=_connect, api=_api)
    assert out["errors"] == ["OpenAEV: OpenAEV refused the request (401 Unauthorized)"]
    assert out["read"] == 1 and out["recorded"] == 1


@pytest.mark.asyncio
async def test_a_failed_findings_route_leaves_the_other_routes_going(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(sessions: Any, *, windows: Any, **kw: Any) -> dict[str, Any]:
        return {"read": 0, "recorded": 0, "errors": []}

    async def _world(sessions: Any, *, windows: Any) -> dict[str, Any]:
        return {}

    async def _boom(sessions: Any, *, windows: Any, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("OpenAEV down")

    async def _passes(sessions: Any) -> dict[str, Any]:
        return {"attack_surface": {"recorded": 1}}

    async def _by_path(sessions: Any) -> dict[str, Any]:
        return {}

    for name in ("resync_pir", "resync_third_party", "resync_aikido", "resync_github"):
        monkeypatch.setattr(resync, name, _ok)
    monkeypatch.setattr(resync, "resync_world", _world)
    monkeypatch.setattr(resync, "resync_openaev_findings", _boom)
    monkeypatch.setattr(resync, "resync_passes", _passes)
    monkeypatch.setattr(resync, "by_trigger_path", _by_path)

    async def _progress(key: str, n: int) -> None:
        return None

    out = await resync.resync(sessions=None, actor_id="admin-1", progress=_progress)  # type: ignore[arg-type]
    routes = out["routes"]
    assert routes["openaev_findings"]["errors"] == ["RuntimeError: OpenAEV down"]
    assert routes["attack_surface"]["recorded"] == 1
    # Simulation results and score degradations stay event only, with the reason why.
    assert "run a simulation" in routes["openaev"]["skipped"]


def test_the_findings_route_is_an_inside_route_with_no_age_limit_by_default() -> None:
    described = {r["key"]: r for r in resync.describe_windows()}
    assert described["openaev_findings"]["group"] == "inside" and described["openaev_findings"]["windowed"] is True
    assert resync.default_windows()["openaev_findings"] is None
    assert described["openaev"]["windowed"] is False and "openaev" in resync.UNLISTED
