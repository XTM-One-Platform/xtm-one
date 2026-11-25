"""CTEM cases on the OpenAEV finding identity (OpenAEV #9233, xtm-one #1994).

- The open read takes CVE findings OPEN and last seen in the 90-day window.
- A member closes at source only on NOT_REPRODUCED or RESOLVED; an OPEN member out of the
  window, a 403 and a 404 never close it.
- A member OpenAEV answers 404 for is matched to the new finding with the same CVE on the
  same asset, or retired when none matches; a failed search decides nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import ctem_cases_source as source

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class _Resp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def json(self) -> Any:
        return self._body


@pytest.mark.asyncio
async def test_the_open_read_takes_open_findings_last_seen_in_the_window(monkeypatch):
    bodies: list[dict] = []

    async def api(base_url, headers, method, path, what, *, json=None, **kw):
        bodies.append(json or {})
        return _Resp({"content": [], "totalPages": 0}), None

    monkeypatch.setattr(source, "_api", api)
    assert await source._read_openaev(None, "https://aev", {}, NOW) == []
    filters = bodies[0]["filterGroup"]["filters"]
    by_key = {f["key"]: f for f in filters}
    assert by_key["finding_type"]["values"] == ["cve"]
    assert by_key["finding_status"]["values"] == ["OPEN"]
    assert by_key["finding_updated_at"]["operator"] == "gte"
    assert by_key["finding_updated_at"]["values"] == ["2026-08-27T12:00:00.000Z"]
    assert "finding_created_at" not in by_key


def _get(fid: str, status: str | None) -> dict:
    return {"finding_id": fid, "finding_type": "cve", "finding_value": f"CVE-2026-{fid}", "finding_status": status}


@pytest.mark.asyncio
async def test_a_member_closes_only_on_not_reproduced_or_resolved(monkeypatch):
    answers = {
        "res": (_Resp(_get("res", "RESOLVED")), None),
        "nr": (_Resp(_get("nr", "NOT_REPRODUCED")), None),
        "old": (_Resp(_get("old", "OPEN")), None),
        "hidden": (None, "403 Forbidden"),
        "gone": (None, "404 Not Found"),
    }

    async def api(base_url, headers, method, path, what, **kw):
        return answers[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(source, "_api", api)
    out = source.SourceRead()
    members = [f"openaev:{k}" for k in answers]
    await source._read_closed_members(out, {"openaev": ("https://aev", {})}, members)
    assert sorted(i.member_id for i in out.closed) == ["openaev:nr", "openaev:res"]
    # An OPEN finding last seen before the window is shown, never resolved.
    assert [i.member_id for i in out.looked_up] == ["openaev:old"]
    assert out.gone == ["openaev:gone"]
    assert out.complete


class _Db:
    def __init__(self, rows: list[tuple[str, dict]]) -> None:
        self._rows = rows

    async def execute(self, statement: Any) -> Any:
        rows = self._rows

        class _Result:
            def all(self) -> list:
                return rows

        return _Result()


def _row(fid: str, cve: str, *assets: tuple[str, str]) -> dict:
    return {
        "finding_id": fid,
        "finding_type": "cve",
        "finding_value": cve,
        "finding_assets": [{"asset_id": i, "asset_name": n} for i, n in assets],
    }


@pytest.mark.asyncio
async def test_a_gone_member_is_remapped_to_the_same_cve_on_the_same_asset(monkeypatch):
    searched: list[str] = []

    async def api(base_url, headers, method, path, what, *, json=None, **kw):
        cve = next(f["values"][0] for f in json["filterGroup"]["filters"] if f["key"] == "finding_value")
        searched.append(cve)
        if cve == "CVE-3":
            return None, "503 Service Unavailable"
        content = {
            "CVE-1": [_row("n1", "CVE-1", ("a-1", "web-1")), _row("n2", "CVE-1", ("a-2", "db-1"))],
            "CVE-2": [_row("n3", "CVE-2", ("a-9", "other"))],
            "CVE-4": [_row("n4", "CVE-4"), _row("n5", "CVE-4", ("a-1", "web-1"))],
        }[cve]
        return _Resp({"content": content, "totalPages": 1}), None

    monkeypatch.setattr(source, "_api", api)
    db = _Db(
        [
            ("o1", {"cve": "CVE-1", "assets": ["db-1"]}),
            ("o2", {"cve": "CVE-2", "assets": ["web-1"]}),
            ("o3", {"cve": "CVE-3", "assets": ["web-1"]}),
            ("o4", {"cve": "CVE-4"}),
        ]
    )
    out = source.SourceRead(gone=["openaev:o1", "openaev:o2", "openaev:o3", "openaev:o4", "openaev:o5"])
    await source._remap_gone_openaev(db, out, ("https://aev", {}))
    assert out.remap == {
        "openaev:o1": "openaev:n2",  # same CVE, same asset
        "openaev:o2": None,  # the CVE is found on another asset only: retired
        # o3: the search failed, nothing is decided
        "openaev:o4": "openaev:n4",  # no asset before, the no-location finding now
        "openaev:o5": None,  # no signal names its CVE: retired
    }
    assert sorted(searched) == ["CVE-1", "CVE-2", "CVE-3", "CVE-4"]


@pytest.mark.asyncio
async def test_the_remap_runs_only_when_openaev_answered(monkeypatch):
    called: list[bool] = []

    async def remap(db, out, cred):
        called.append(True)

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(source, "_remap_gone_openaev", remap)
    monkeypatch.setattr(source, "_organization_software", nothing)
    monkeypatch.setattr(source, "_user_creds", nothing)
    read = await source.read_open_issues(None, "u", "cyber", remap=True)
    assert "openaev" in read.missing and called == []
