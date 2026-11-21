"""Host response actions: Defender Live Response, EC2 isolation, OVHcloud IP block.

Sam's decision (21 Nov 2026, #863): each action exists, asks a person on every
call (``always_ask``) and has an undo. The tools run for real against fake
APIs (httpx.MockTransport or a captured signed request), no network.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations.providers import aws_security, defender, ovhcloud
from app.integrations.providers.ovhcloud import _common
from app.integrations.providers.ovhcloud._common import build_ovhcloud_token
from app.services import web_fetch
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor

GATED = {
    "run_defender_live_response_script",
    "isolate_aws_ec2_instance",
    "release_aws_ec2_instance_isolation",
    "block_ovhcloud_ip",
    "unblock_ovhcloud_ip",
}


def _definitions() -> dict[str, Any]:
    defs = []
    defs += defender.build_tools("", CallableToolExecutor(), token_acquirer=AsyncMock(return_value="tok"))
    defs += aws_security.build_tools("us-east-1||AKIA_TEST||secret", CallableToolExecutor())
    defs += ovhcloud.build_tools(
        build_ovhcloud_token("ovh-eu", "application_key", "AK", "CK", "AS"), CallableToolExecutor()
    )
    return {d.name: d for d in defs}


def test_every_host_action_asks_a_person_on_every_call_and_is_a_write():
    defs = _definitions()
    for name in GATED:
        assert defs[name].always_ask, name
        assert infer_tool_type(name) == "write", name
    for name in ("list_defender_live_response_library", "get_defender_live_response_result"):
        assert not defs[name].always_ask and infer_tool_type(name) == "read", name


# --- Defender Live Response -------------------------------------------------

MDE = "api.securitycenter.microsoft.com"
BLOB = "blob.example.net"


class _Fake:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.url.host, request.url.path))
        if response is None:
            raise AssertionError(f"no fake route for {request.method} {request.url}")
        return response(request) if callable(response) else response


@pytest.fixture
def mde(monkeypatch) -> _Fake:
    fake = _Fake()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    real_policy = web_fetch.validate_outbound_url

    def _policy(url: str, *, allow_private: bool = False) -> str:
        try:
            ipaddress.ip_address(httpx.URL(url).host)
        except ValueError:
            return url
        return real_policy(url, allow_private=allow_private)

    monkeypatch.setattr(web_fetch, "validate_outbound_url", _policy)
    return fake


def _defender() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    defender.build_tools("", executor, token_acquirer=AsyncMock(return_value="tok"))
    return executor


@pytest.mark.asyncio
async def test_live_response_runs_a_library_script_and_names_the_undo(mde):
    mde.routes[(MDE, "/api/machines/M1/runliveresponse")] = httpx.Response(201, json={"id": "A9", "status": "Pending"})

    out = await _defender().handler("run_defender_live_response_script")(
        machine_id="M1", script_name="who-listens.ps1", args="-Port 443", comment="find the listener"
    )

    assert "action id: A9" in out and "cancel_defender_machine_action" in out
    body = json.loads(mde.requests[0].content)
    assert body["Commands"] == [
        {
            "type": "RunScript",
            "params": [{"key": "ScriptName", "value": "who-listens.ps1"}, {"key": "Args", "value": "-Port 443"}],
        }
    ]


@pytest.mark.asyncio
async def test_live_response_refuses_a_path_or_a_missing_reason_before_any_request(mde):
    run = _defender().handler("run_defender_live_response_script")
    assert (await run(machine_id="M1", script_name="../x.ps1", comment="c")).startswith("Error")
    assert (await run(machine_id="M1", script_name="x.ps1", comment=" ")).startswith("Error")
    assert mde.requests == []


@pytest.mark.asyncio
async def test_live_response_result_reads_the_script_output(mde):
    mde.routes[(MDE, "/api/machineactions/A9")] = httpx.Response(
        200, json={"id": "A9", "type": "LiveResponse", "status": "Succeeded", "computerDnsName": "web-01"}
    )
    mde.routes[(MDE, "/api/machineactions/A9/GetLiveResponseResultDownloadLink(index=0)")] = httpx.Response(
        200, json={"value": f"https://{BLOB}/result.json"}
    )
    mde.routes[(BLOB, "/result.json")] = httpx.Response(
        200, json={"script_name": "who-listens.ps1", "exit_code": 0, "script_output": "nginx 1234", "script_errors": ""}
    )

    out = await _defender().handler("get_defender_live_response_result")(action_id="A9")

    assert "exit code 0" in out and "nginx 1234" in out and "web-01" in out
    download = next(r for r in mde.requests if r.url.host == BLOB)
    assert "authorization" not in {k.lower() for k in download.headers}


@pytest.mark.asyncio
async def test_live_response_result_waits_for_a_pending_run(mde):
    mde.routes[(MDE, "/api/machineactions/A9")] = httpx.Response(
        200, json={"id": "A9", "type": "LiveResponse", "status": "InProgress"}
    )
    out = await _defender().handler("get_defender_live_response_result")(action_id="A9")
    assert "not finished (status: InProgress)" in out


# --- AWS EC2 isolation --------------------------------------------------------

_NS = 'xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"'


class _Resp:
    def __init__(self, xml: str) -> None:
        self.status_code = 200
        self.content = xml.encode()


def _instance_xml(groups: list[str], enis: int = 1) -> str:
    group_items = "".join(f"<item><groupId>{g}</groupId></item>" for g in groups)
    eni_items = "<item><networkInterfaceId>eni-1</networkInterfaceId></item>" * enis
    return (
        f"<DescribeInstancesResponse {_NS}><reservationSet><item><instancesSet><item>"
        f"<instanceId>i-0abc12345678</instanceId><vpcId>vpc-1</vpcId>"
        f"<groupSet>{group_items}</groupSet><networkInterfaceSet>{eni_items}</networkInterfaceSet>"
        "</item></instancesSet></item></reservationSet></DescribeInstancesResponse>"
    )


def _aws(monkeypatch, answers: dict[str, str]) -> tuple[list[dict[str, list[str]]], CallableToolExecutor]:
    calls: list[dict[str, list[str]]] = []

    async def _signed(**kwargs):
        form = parse_qs(kwargs["raw_body"].decode())
        calls.append(form)
        return _Resp(answers.get(form["Action"][0], f"<Ok {_NS}/>")), None

    monkeypatch.setattr(aws_security, "_signed_request", _signed)
    executor = CallableToolExecutor()
    aws_security.build_tools("us-east-1||AKIA_TEST||secret", executor)
    return calls, executor


@pytest.mark.asyncio
async def test_isolation_replaces_the_groups_and_records_them(monkeypatch):
    calls, executor = _aws(
        monkeypatch,
        {
            "DescribeInstances": _instance_xml(["sg-a", "sg-b"]),
            "CreateSecurityGroup": f"<CreateSecurityGroupResponse {_NS}><groupId>sg-iso</groupId></CreateSecurityGroupResponse>",
        },
    )

    out = await executor.handler("isolate_aws_ec2_instance")(instance_id="i-0abc12345678", reason="beaconing")

    actions = [c["Action"][0] for c in calls]
    assert actions == [
        "DescribeInstances",
        "CreateSecurityGroup",
        "RevokeSecurityGroupEgress",
        "ModifyInstanceAttribute",
    ]
    create = calls[1]
    assert create["TagSpecification.1.Tag.1.Value"] == ["sg-a,sg-b"]
    assert calls[3]["GroupId.1"] == ["sg-iso"] and "GroupId.2" not in calls[3]
    assert "sg-iso" in out and "release_aws_ec2_instance_isolation" in out


@pytest.mark.asyncio
async def test_isolation_fits_the_reason_to_the_group_description_charset(monkeypatch):
    calls, executor = _aws(
        monkeypatch,
        {
            "DescribeInstances": _instance_xml(["sg-a"]),
            "CreateSecurityGroup": f"<CreateSecurityGroupResponse {_NS}><groupId>sg-iso</groupId></CreateSecurityGroupResponse>",
        },
    )
    reason = "l'hôte beacons\nto C2 " + "x" * 300

    out = await executor.handler("isolate_aws_ec2_instance")(instance_id="i-0abc12345678", reason=reason)

    create = next(c for c in calls if c["Action"][0] == "CreateSecurityGroup")
    description = create["GroupDescription"][0]
    assert len(description) <= 255 and "…" not in description
    assert re.fullmatch(r"[a-zA-Z0-9 ._\-:/()#,@\[\]+=&;{}!$*]+", description)
    assert description.startswith("Isolation of i-0abc12345678: l h te beacons to C2 ")
    assert reason.strip() in out


@pytest.mark.asyncio
async def test_isolation_refuses_a_bad_id_and_a_multi_interface_instance(monkeypatch):
    calls, executor = _aws(monkeypatch, {"DescribeInstances": _instance_xml(["sg-a"], enis=2)})
    isolate = executor.handler("isolate_aws_ec2_instance")
    assert (await isolate(instance_id="not-an-id", reason="r")).startswith("Error")
    assert calls == []
    out = await isolate(instance_id="i-0abc12345678", reason="r")
    assert out.startswith("Error") and "2 network interfaces" in out
    assert [c["Action"][0] for c in calls] == ["DescribeInstances"]


@pytest.mark.asyncio
async def test_release_restores_the_recorded_groups(monkeypatch):
    calls, executor = _aws(
        monkeypatch,
        {
            "DescribeInstances": _instance_xml(["sg-iso"]),
            "DescribeSecurityGroups": (
                f"<DescribeSecurityGroupsResponse {_NS}><securityGroupInfo><item><groupId>sg-iso</groupId>"
                "<tagSet><item><key>xtm-one:isolated-from-groups</key><value>sg-a,sg-b</value></item></tagSet>"
                "</item></securityGroupInfo></DescribeSecurityGroupsResponse>"
            ),
        },
    )

    out = await executor.handler("release_aws_ec2_instance_isolation")(instance_id="i-0abc12345678")

    modify = next(c for c in calls if c["Action"][0] == "ModifyInstanceAttribute")
    assert modify["GroupId.1"] == ["sg-a"] and modify["GroupId.2"] == ["sg-b"]
    assert calls[-1]["Action"] == ["DeleteSecurityGroup"]
    assert "restored" in out


# --- OVHcloud IP block ----------------------------------------------------------


@pytest.fixture
def ovh(monkeypatch):
    _common.clear_time_deltas()
    requests: list[httpx.Request] = []
    routes: dict[tuple[str, str], Any] = {}

    def _answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0].removeprefix("/1.0")
        if path == "/auth/time":
            return httpx.Response(200, json=1)
        return httpx.Response(200, json=routes.get((request.method, path), {}))

    transport = httpx.MockTransport(_answer)

    def _client(*args, **kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(*args, transport=transport, **kwargs)

    monkeypatch.setattr(_common, "egress_client", _client)
    executor = CallableToolExecutor()
    ovhcloud.build_tools(build_ovhcloud_token("ovh-eu", "application_key", "AK", "CK", "AS"), executor)
    yield routes, requests, executor
    _common.clear_time_deltas()


def _writes(requests: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(r.method, r.url.raw_path.decode().removeprefix("/1.0")) for r in requests if r.method != "GET"]


@pytest.mark.asyncio
async def test_block_adds_the_ip_a_deny_all_rule_and_enables_the_firewall(ovh):
    routes, requests, executor = ovh
    routes[("GET", "/ip/203.0.113.0%2F28/firewall")] = []
    routes[("GET", "/ip/203.0.113.0%2F28/firewall/203.0.113.4/rule")] = []

    out = await executor.handler("block_ovhcloud_ip")(ip_block="203.0.113.0/28", ip="203.0.113.4", reason="c2")

    assert _writes(requests) == [
        ("POST", "/ip/203.0.113.0%2F28/firewall"),
        ("POST", "/ip/203.0.113.0%2F28/firewall/203.0.113.4/rule"),
        ("PUT", "/ip/203.0.113.0%2F28/firewall/203.0.113.4"),
    ]
    rule = json.loads(next(r for r in requests if r.url.raw_path.endswith(b"/rule") and r.method == "POST").content)
    assert rule == {"action": "deny", "protocol": "ipv4", "sequence": 0}
    assert "unblock_ovhcloud_ip" in out


@pytest.mark.asyncio
async def test_block_never_overwrites_an_existing_first_rule(ovh):
    routes, requests, executor = ovh
    routes[("GET", "/ip/203.0.113.0%2F28/firewall")] = ["203.0.113.4"]
    routes[("GET", "/ip/203.0.113.0%2F28/firewall/203.0.113.4/rule")] = [0, 1]

    out = await executor.handler("block_ovhcloud_ip")(ip_block="203.0.113.0/28", ip="203.0.113.4", reason="c2")

    assert out.startswith("Error") and _writes(requests) == []


@pytest.mark.asyncio
async def test_unblock_removes_only_the_deny_all_rule(ovh):
    routes, requests, executor = ovh
    rule = "/ip/203.0.113.0%2F28/firewall/203.0.113.4/rule/0"
    routes[("GET", rule)] = {"action": "deny", "protocol": "ipv4", "sequence": 0}
    unblock = executor.handler("unblock_ovhcloud_ip")

    out = await unblock(ip_block="203.0.113.0/28", ip="203.0.113.4")
    assert _writes(requests) == [("DELETE", rule)] and "unblocked" in out

    requests.clear()
    routes[("GET", rule)] = {"action": "deny", "protocol": "tcp", "sequence": 0, "destinationPort": "eq 22"}
    out = await unblock(ip_block="203.0.113.0/28", ip="203.0.113.4")
    assert out.startswith("Error") and _writes(requests) == []

    routes[("GET", rule)] = {"action": "deny", "protocol": "udp", "sequence": 0}
    out = await unblock(ip_block="203.0.113.0/28", ip="203.0.113.4")
    assert out.startswith("Error") and _writes(requests) == []
