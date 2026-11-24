"""Intune, Tanium, CrowdStrike Falcon and ServiceNow as asset group sources (#1556): mapping, members, paging."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from app.services import organization_context_groups as groups
from app.services import organization_context_import as imp
from app.services.organization_context import AssetGroupEntry


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


class _Client:
    """Answers by URL prefix; records every call."""

    def __init__(self, routes: list[tuple[str, Any]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append((method, url, kwargs))
        for prefix, answer in self.routes:
            if prefix in url:
                body = answer(url, kwargs) if callable(answer) else answer
                return body if isinstance(body, _Resp) else _Resp(200, body)
        return _Resp(404, {})


def test_sources_are_offered_for_asset_groups_with_a_mapping() -> None:
    for source in groups.SOURCES:
        assert source in imp.SECTION_SOURCES["asset_groups"]
        assert imp.SOURCE_FILTERS[("asset_groups", source)] == groups.FILTERS[source]
    assert imp.SECTION_SOURCES["asset_groups"][0] == "openaev"


@pytest.mark.parametrize(
    ("row", "source", "tags"),
    [
        (
            groups.intune_group_row(
                {"id": "g1", "displayName": "Finance laptops", "description": "d", "groupTypes": ["DynamicMembership"]}
            ),
            "intune",
            ["entra:dynamic"],
        ),
        (groups.tanium_group_row({"id": "12", "name": "All Linux", "type": "STANDARD"}), "tanium", ["tanium:standard"]),
        (
            groups.falcon_group_row({"id": "hg1", "name": "Servers", "group_type": "dynamic"}),
            "crowdstrike",
            ["falcon:dynamic"],
        ),
        (
            groups.servicenow_group_row(
                {
                    "sys_id": {"value": "s1", "display_value": "s1"},
                    "name": {"value": "Payroll", "display_value": "Payroll"},
                }
            ),
            "servicenow",
            ["servicenow:business_service"],
        ),
    ],
)
def test_group_rows_are_asset_group_entries(row: dict[str, Any], source: str, tags: list[str]) -> None:
    entry = AssetGroupEntry.model_validate(row)
    assert entry.source == source and entry.external_id and entry.name
    assert entry.tags == tags


@pytest.mark.parametrize(
    ("source", "error", "reason"),
    [
        (
            "crowdstrike",
            groups.missing_scope_message("crowdstrike", 401),
            "CrowdStrike Falcon refused the group read (HTTP 401): it needs an API client with the "
            "Host groups: Read and Hosts: Read scopes.",
        ),
        ("tanium", "tanium: no Tanium integration is connected", "No Tanium integration is connected."),
        ("servicenow", "servicenow: HTTP 503", "ServiceNow answered with HTTP status 503."),
        ("intune", "intune: ConnectTimeout", "Microsoft Intune did not answer in time."),
        ("crowdstrike", "crowdstrike: KeyError", "CrowdStrike Falcon could not be reached."),
        ("opencti", "OpenCTI: upstream 502 tenant 42", None),
        ("servicenow", None, None),
    ],
)
def test_preview_error_reason_names_the_cause_never_a_raw_body(
    source: str, error: str | None, reason: str | None
) -> None:
    """UX round 1 B1 on #1627: the permission reaches the screen; the source key and raw text never do."""
    assert imp.readable_error(source, error) == reason
    plan = imp.ImportPlan(section="asset_groups", source=source, source_total=None, read=0, error=error)
    assert plan.as_dict()["error_reason"] == reason


def test_a_group_without_id_or_name_is_left_out() -> None:
    assert groups.tanium_group_row({"id": "", "name": "x"}) == {}
    assert groups.falcon_group_row({"id": "a", "name": " "}) == {}


def test_intune_members_map_entra_devices_to_managed_ids() -> None:
    managed = {"aaaa": "md-1", "bbbb": "md-2"}
    devices = [{"deviceId": "AAAA"}, {"deviceId": "cccc"}, {"deviceId": "aaaa"}, {"deviceId": "bbbb"}]
    assert groups.intune_members(devices, managed) == ["md-1", "md-2"]


def test_read_intune_groups_skips_user_groups_and_keeps_device_members() -> None:
    client = _Client(
        [
            ("/deviceManagement/managedDevices", {"value": [{"id": "md-1", "azureADDeviceId": "AAAA"}]}),
            ("/groups/g-dev/members", {"value": [{"deviceId": "aaaa"}, {"deviceId": "zzzz"}]}),
            ("/groups/g-users/members", {"value": []}),
            (
                "/groups?",
                {"value": [{"id": "g-dev", "displayName": "Laptops"}, {"id": "g-users", "displayName": "Staff"}]},
            ),
        ]
    )
    read = asyncio.run(groups.read_intune_groups("t", {}, limit=100, client=client))
    assert read.error is None
    assert [r["external_id"] for r in read.rows] == ["g-dev"]
    assert read.members == {"g-dev": ["md-1"]}


def test_read_intune_groups_names_the_permission_on_403() -> None:
    client = _Client([("/deviceManagement/managedDevices", _Resp(403, {}))])
    read = asyncio.run(groups.read_intune_groups("t", {}, limit=100, client=client))
    assert read.error and "GroupMember.Read.All" in read.error


def test_read_tanium_groups_pages_members_and_filters_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers import tanium

    monkeypatch.setattr(tanium, "_parse", lambda token: ("https://t.example", "tok"))

    def answer(url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        body = kwargs["json"]
        if "computerGroups" in body["query"]:
            nodes = [{"id": "1", "name": "All Linux", "type": "STANDARD"}, {"id": "2", "name": "All Windows"}]
            return {"data": {"computerGroups": {"pageInfo": {}, "edges": [{"node": n} for n in nodes]}}}
        after = body["variables"]["after"]
        if after is None:
            page = {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "edges": [{"node": {"id": "e1"}}]}
        else:
            page = {"pageInfo": {"hasNextPage": False}, "edges": [{"node": {"id": "e2"}}, {"node": {"id": "e1"}}]}
        return {"data": {"endpoints": page}}

    client = _Client([("https://t.example", answer)])
    read = asyncio.run(groups.read_tanium_groups("x", {"search": "linux"}, limit=100, client=client))
    assert read.error is None
    assert [r["name"] for r in read.rows] == ["All Linux"]
    assert read.members == {"1": ["e1", "e2"]}


def test_read_crowdstrike_groups_reads_members_and_keeps_a_failed_group_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers import crowdstrike

    async def _token(*_a: Any) -> tuple[str, int]:
        return "bearer", 1800

    monkeypatch.setattr(crowdstrike, "_request_token_with_ttl", _token)

    def members(url: str, kwargs: dict[str, Any]) -> Any:
        if kwargs["params"]["id"] == "hg2":
            return _Resp(500, {})
        return {"resources": ["aid1", "aid2"], "meta": {"pagination": {"total": 2}}}

    client = _Client(
        [
            ("/devices/queries/host-group-members/v1", members),
            (
                "/devices/combined/host-groups/v1",
                {
                    "resources": [{"id": "hg1", "name": "Servers"}, {"id": "hg2", "name": "Laptops"}],
                    "meta": {"pagination": {"total": 2}},
                },
            ),
        ]
    )
    read = asyncio.run(
        groups.read_crowdstrike_groups("https://api.crowdstrike.com|id|secret", {}, limit=100, client=client)
    )
    assert read.error is None
    assert [r["external_id"] for r in read.rows] == ["hg1", "hg2"]
    assert read.members == {"hg1": ["aid1", "aid2"]}
    assert read.members_unread == {"hg2"}


def test_read_servicenow_groups_reads_hardware_children_of_each_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.servicenow import _common

    monkeypatch.setattr(_common, "parse_servicenow_token", lambda token: ("https://sn.example", "u", "p"))
    client = _Client(
        [
            (
                "/cmdb_rel_ci",
                {
                    "result": [
                        {"parent": "s1", "child": "ci1"},
                        {"parent": "s1", "child": "ci2"},
                        {"parent": "s9", "child": "x"},
                    ]
                },
            ),
            (
                "/cmdb_ci_service",
                {
                    "result": [
                        {"sys_id": {"value": "s1"}, "name": {"value": "Payroll", "display_value": "Payroll"}},
                        {"sys_id": {"value": "s2"}, "name": {"value": "Web", "display_value": "Web"}},
                    ]
                },
            ),
        ]
    )
    read = asyncio.run(groups.read_servicenow_groups("t", {}, limit=100, client=client))
    assert read.error is None
    assert read.members == {"s1": ["ci1", "ci2"], "s2": []}
    rel = next(c for c in client.calls if "/cmdb_rel_ci" in c[1])
    assert "INSTANCEOFcmdb_ci_hardware" in rel[2]["params"]["sysparm_query"]


def test_read_groups_keeps_only_whole_member_lists_and_names_the_member_source(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _read(db: Any, user_id: str, source: str, filters: dict[str, Any], *, limit: int) -> groups.GroupRead:
        return groups.GroupRead(
            rows=[groups.group_row(source, "a", "A"), groups.group_row(source, "b", "B")],
            members={"a": ["m1"]},
            members_unread={"b"},
            total=2,
        )

    monkeypatch.setattr(groups, "read_groups", _read)
    read = asyncio.run(imp._read_groups(cast(Any, None), "u", "crowdstrike", {}))
    assert [r.external_id for r in read.rows] == ["a", "b"]
    assert read.members == {"a": ["m1"]}
    assert read.members_source == "crowdstrike"
