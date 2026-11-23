# SPDX-License-Identifier: Apache-2.0

"""Repository dependencies matched to OpenCTI vulnerabilities, PURL first (#1331, slice 4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals
from app.services import ctem_dependency_matches as dm

ORG = uuid.uuid4()
REPOS = {"77": {"name": "acme/web", "identity_key": "github.com:repository:77"}}
SOFTWARE = {
    "77": [
        {"product": "lodash", "version": "4.17.20", "purl": "pkg:npm/lodash@4.17.20"},
        {"product": "left-pad", "version": "unknown", "purl": "pkg:npm/left-pad"},
        {"product": "no-purl", "version": "1.0.0", "purl": ""},
        {"product": "@babel/core", "version": "7.1.0", "purl": "pkg:npm/%40babel/core@7.1.0?arch=x"},
    ]
}


def test_purl_parts_drops_qualifiers_and_keeps_the_version() -> None:
    assert dm.purl_parts("pkg:npm/%40babel/core@7.1.0?arch=x#sub") == ("pkg:npm/@babel/core", "7.1.0")
    assert dm.purl_parts("pkg:maven/org.apache/log4j-core@2.14.1") == ("pkg:maven/org.apache/log4j-core", "2.14.1")
    assert dm.purl_parts("pkg:npm/@babel/core") == ("pkg:npm/@babel/core", "")
    assert dm.purl_parts("lodash@4") == ("", "")


def test_only_components_with_a_purl_and_a_known_version_are_matched() -> None:
    comps = dm.components(SOFTWARE, REPOS)
    assert sorted(c.product for c in comps) == ["@babel/core", "lodash"]
    lodash = next(c for c in comps if c.product == "lodash")
    assert (lodash.repository, lodash.identity_key, lodash.purl) == (
        "acme/web",
        "github.com:repository:77",
        "pkg:npm/lodash@4.17.20",
    )


def test_opencti_software_matches_on_the_purl_and_the_version() -> None:
    comps = dm.components(SOFTWARE, REPOS)
    found = [
        {"id": "s1", "x_opencti_purl": "pkg:npm/lodash@4.17.20"},
        {"id": "s2", "x_opencti_purl": "pkg:npm/lodash", "version": "4.17.21"},  # another version
        {"id": "s3", "x_opencti_purl": "pkg:npm/%40babel/core", "version": "7.1.0"},
        {"id": "s4", "name": "lodash", "version": "4.17.20"},  # no PURL: never matched by name
    ]
    matched = dm.match_software(found, comps)
    assert sorted(matched) == ["s1", "s3"]


def test_a_vulnerable_component_is_a_path_3_signal_on_its_repository_with_its_fix() -> None:
    lodash = next(c for c in dm.components(SOFTWARE, REPOS) if c.product == "lodash")
    fixes = dm.fixes_by_vulnerability(
        [
            {"from": {"x_opencti_purl": "pkg:npm/lodash@4.17.21"}, "to": {"id": "v1"}},
            {"from": {"version": "4.17.19"}, "to": {"id": "v1"}},
            {"from": {"x_opencti_purl": "pkg:npm/other@9.0.0"}, "to": {"id": "v1"}},
        ]
    )
    fixed = dm.fixed_version_for(lodash, fixes["v1"])
    assert fixed == "4.17.21"
    fields = dm.signal_fields(lodash, {"id": "v1", "name": "CVE-2021-23337", "x_opencti_cisa_kev": True}, fixed)
    assert fields is not None
    evidence = fields["evidence"]
    assert evidence["assets"] == ["acme/web", "77", "github.com:repository:77"]
    assert (evidence["affected_package"], evidence["fixed_version"], evidence["repository"]) == (
        "lodash",
        "4.17.21",
        "acme/web",
    )
    assert evidence["cve_id"] == "CVE-2021-23337" and evidence["kev"] is True
    assert fields["external_id"] == "github.com:repository:77|pkg:npm/lodash|v1"
    assert dm.signal_fields(lodash, {"name": "no id"}, None) is None


def test_the_case_reads_upgrade_library_to_fixed_version_in_repository() -> None:
    lodash = next(c for c in dm.components(SOFTWARE, REPOS) if c.product == "lodash")
    fields = dm.signal_fields(lodash, {"id": "v1", "name": "CVE-2021-23337"}, "4.17.21")
    assert fields is not None
    signal = SimpleNamespace(
        subject_kind="code",
        subject_ref="lodash",
        subject_label="lodash",
        trigger_path="code_flaw",
        evidence=fields["evidence"],
    )
    fix = ctem_case_loop.fix_of(signal)  # type: ignore[arg-type]
    assert fix.title == "Upgrade lodash to 4.17.21 in acme/web" and fix.fixed_version == "4.17.21"


def test_no_fix_known_is_never_guessed() -> None:
    lodash = next(c for c in dm.components(SOFTWARE, REPOS) if c.product == "lodash")
    assert dm.fixed_version_for(lodash, [("pkg:npm/lodash", "4.17.19")]) is None
    fields = dm.signal_fields(lodash, {"id": "v1", "name": "GHSA-x"}, None)
    assert fields is not None and "fixed_version" not in fields["evidence"]


def test_the_remediates_query_selects_the_purl() -> None:
    assert "x_opencti_purl" in dm._OCTI_SOFTWARE_REMEDIATES_PURL


def test_a_fix_of_another_package_is_never_the_fixed_version() -> None:
    lodash = next(c for c in dm.components(SOFTWARE, REPOS) if c.product == "lodash")
    # The node shape the remediates query answers: id, name, version, x_opencti_purl.
    fixes = dm.fixes_by_vulnerability(
        [
            {"from": {"id": "a", "name": "other", "version": "4.17.22", "x_opencti_purl": None}, "to": {"id": "v1"}},
            {
                "from": {"id": "b", "name": "other", "version": "9.0.0", "x_opencti_purl": "pkg:npm/other@9.0.0"},
                "to": {"id": "v1"},
            },
            {"from": {"id": "c", "name": None, "version": "4.17.21", "x_opencti_purl": None}, "to": {"id": "v1"}},
        ]
    )
    assert dm.fixed_version_for(lodash, fixes["v1"]) is None
    fixes = dm.fixes_by_vulnerability(
        [
            {"from": {"id": "d", "name": "Lodash", "version": "4.17.23", "x_opencti_purl": None}, "to": {"id": "v1"}},
            {
                "from": {"id": "e", "name": "lodash", "version": "4.17.21", "x_opencti_purl": "pkg:npm/lodash@4.17.21"},
                "to": {"id": "v1"},
            },
        ]
    )
    assert dm.fixed_version_for(lodash, fixes["v1"]) == "4.17.21"


class _Db:
    def __init__(self) -> None:
        self.sync_session = object()

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield


def _db() -> AsyncSession:
    return cast(AsyncSession, _Db())


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)


@pytest.mark.usefixtures("xtm_one")
async def test_record_matches_records_through_the_intake(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_code_findings, ctem_metrics_collector

    recorded: list[Any] = []

    async def _record(_db: Any, **kw: Any) -> tuple[Any, bool]:
        recorded.append(kw)
        return SimpleNamespace(id=uuid.uuid4()), True

    async def _gate(_db: Any, _signal: Any, *, created: bool) -> ctem_case_loop.PendingEvents:
        return ctem_case_loop.PendingEvents()

    async def _creds(_db: Any, _user: Any, _provider: Any) -> tuple[str, dict[str, str]]:
        return "https://octi", {"Authorization": "Bearer t"}

    async def _read(_client: Any, _base: Any, _headers: Any, wanted: list[dm.Component]) -> list[Any]:
        lodash = next(c for c in wanted if c.product == "lodash")
        return [(lodash, {"id": "v1", "name": "CVE-2021-23337"}, "4.17.21")]

    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _gate)
    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _creds)
    monkeypatch.setattr(dm, "read_matches", _read)
    monkeypatch.setattr(ctem_code_findings, "_publish_after_commit", lambda _db, _p: None)

    counts = await dm.record_matches(
        _db(), organization_id=ORG, user_id=str(uuid.uuid4()), software=SOFTWARE, repositories=REPOS
    )
    assert counts == {"components": 2, "recorded": 1}
    signal = recorded[0]["signal"]
    assert (signal.trigger_path, signal.lens, signal.subject_kind, signal.source_provider) == (
        "code_flaw",
        "inside",
        "code",
        "opencti",
    )
    assert recorded[0]["recorded_by"] == "system" and signal.evidence["fixed_version"] == "4.17.21"


@pytest.mark.usefixtures("xtm_one")
async def test_no_opencti_or_an_error_records_nothing_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_metrics_collector

    async def _none(_db: Any, _user: Any, _provider: Any) -> None:
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _none)
    counts = await dm.record_matches(_db(), organization_id=ORG, user_id="u", software=SOFTWARE, repositories=REPOS)
    assert counts == {"components": 2, "recorded": 0}

    async def _boom(_db: Any, _user: Any, _provider: Any) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _boom)
    counts = await dm.record_matches(_db(), organization_id=ORG, user_id="u", software=SOFTWARE, repositories=REPOS)
    assert counts["recorded"] == 0


async def test_off_in_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    counts = await dm.record_matches(_db(), organization_id=ORG, user_id="u", software=SOFTWARE, repositories=REPOS)
    assert counts == {"components": 0, "recorded": 0}
