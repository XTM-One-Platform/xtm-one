"""OVHcloud provider: signature, clock offset, both auth modes, bounded reads, write gating."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.integrations.providers import ovhcloud
from app.integrations.providers.ovhcloud import _common
from app.integrations.providers.ovhcloud._common import (
    OvhClient,
    build_ovhcloud_token,
    compute_signature,
    parse_ovhcloud_token,
    redact,
    render,
    token_from_config,
)
from app.integrations.providers.ovhcloud.inventory import cloud_row, dedicated_row, filter_rows
from app.integrations.providers.ovhcloud.network import normalize_api_path
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor

KEY_TOKEN = build_ovhcloud_token("ovh-eu", "application_key", "AK", "CK", "AS")
OAUTH_TOKEN = build_ovhcloud_token("ovh-ca", "oauth2", "client-id", "", "client-secret")

WRITE_TOOLS = {
    "reboot_ovhcloud_dedicated_server",
    "reboot_ovhcloud_cloud_instance",
    "start_ovhcloud_cloud_instance",
    "stop_ovhcloud_cloud_instance",
    "reboot_ovhcloud_vps",
    "block_ovhcloud_ip",
    "unblock_ovhcloud_ip",
}


@pytest.fixture(autouse=True)
def _fresh_clock():
    _common.clear_time_deltas()
    yield
    _common.clear_time_deltas()


class _Recorder:
    """An httpx transport answering by path, recording every request."""

    def __init__(self, routes: dict[str, object], *, status: dict[str, int] | None = None):
        self.routes = routes
        self.status = status or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/1.0")
        code = self.status.get(path, 200)
        if path not in self.routes and code == 200:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(code, json=self.routes.get(path, {"message": "error"}))


@pytest.fixture
def ovh_api(monkeypatch):
    def _install(routes, status=None):
        recorder = _Recorder(routes, status=status)
        transport = httpx.MockTransport(recorder)

        def _client(*args, **kwargs):
            kwargs.pop("transport", None)
            return httpx.AsyncClient(*args, transport=transport, **kwargs)

        monkeypatch.setattr(_common, "egress_client", _client)
        return recorder

    return _install


def _tools(token: str = KEY_TOKEN):
    executor = CallableToolExecutor()
    defs = ovhcloud.build_tools(token, executor)
    return defs, executor


async def _call(executor, name, **kwargs):
    return await executor.handler(name)(**kwargs)


# --- credential --------------------------------------------------------------


def test_signature_matches_the_documented_scheme():
    raw = "AS+CK+GET+https://eu.api.ovh.com/1.0/me++1700000000"
    expected = "$1$" + hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324 - the API's scheme
    assert compute_signature("AS", "CK", "get", "https://eu.api.ovh.com/1.0/me", "", 1700000000) == expected


def test_signature_known_vector():
    # sha1("a+b+GET+u+c+1") computed once, fixed here so a refactor of the join is caught.
    assert compute_signature("a", "b", "GET", "u", "c", 1) == "$1$" + hashlib.sha1(b"a+b+GET+u+c+1").hexdigest()  # noqa: S324
    assert compute_signature("a", "b", "GET", "u", "c", 1).startswith("$1$")
    assert len(compute_signature("a", "b", "GET", "u", "c", 1)) == 43


def test_token_round_trip_and_config_shapes():
    creds = parse_ovhcloud_token(KEY_TOKEN)
    assert (creds.endpoint, creds.mode, creds.key_or_client_id, creds.consumer_key, creds.secret) == (
        "ovh-eu",
        "application_key",
        "AK",
        "CK",
        "AS",
    )
    assert creds.base_url == "https://eu.api.ovh.com/1.0"
    oauth = parse_ovhcloud_token(
        token_from_config(
            "sec", {"endpoint": "ovh-us", "auth_mode": "oauth2", "client_id": "cid", "consumer_key": "ignored"}
        )
    )
    assert (oauth.mode, oauth.key_or_client_id, oauth.consumer_key, oauth.secret) == ("oauth2", "cid", "", "sec")
    assert oauth.base_url == "https://api.us.ovhcloud.com/1.0"
    # an unknown endpoint falls back to Europe, never to a typed host
    assert parse_ovhcloud_token(token_from_config("s", {"endpoint": "https://evil.example"})).endpoint == "ovh-eu"


def test_missing_parts_are_named():
    assert parse_ovhcloud_token(token_from_config("", {})).missing() == [
        "application key",
        "application secret",
        "consumer key",
    ]
    assert parse_ovhcloud_token(token_from_config("s", {"auth_mode": "oauth2"})).missing() == ["client ID"]


# --- requests ----------------------------------------------------------------


async def test_key_mode_signs_with_the_server_clock_offset(ovh_api):
    api = ovh_api({"/auth/time": 1_000_100, "/me": {"nichandle": "ab123-ovh"}})
    client = OvhClient(parse_ovhcloud_token(KEY_TOKEN), clock=lambda: 1_000_000.0)
    data, err = await client.get("/me", "read the account")
    assert err is None and data == {"nichandle": "ab123-ovh"}
    signed = api.requests[-1]
    assert signed.headers["X-Ovh-Application"] == "AK"
    assert signed.headers["X-Ovh-Consumer"] == "CK"
    assert signed.headers["X-Ovh-Timestamp"] == "1000100"
    assert signed.headers["X-Ovh-Signature"] == compute_signature(
        "AS", "CK", "GET", "https://eu.api.ovh.com/1.0/me", "", 1_000_100
    )
    # the offset is cached: a second call does not read /auth/time again
    await client.get("/me", "read the account")
    assert [r.url.path for r in api.requests].count("/1.0/auth/time") == 1


async def test_key_mode_signs_the_query_string_and_body(ovh_api):
    api = ovh_api({"/auth/time": 50, "/dedicated/server/ns1/reboot": {"taskId": 1}})
    client = OvhClient(parse_ovhcloud_token(KEY_TOKEN), clock=lambda: 50.0)
    await client.request("POST", "/dedicated/server/ns1/reboot", "reboot", params={"a": "1"}, body={"x": 1})
    req = api.requests[-1]
    assert req.headers["X-Ovh-Signature"] == compute_signature(
        "AS", "CK", "POST", "https://eu.api.ovh.com/1.0/dedicated/server/ns1/reboot?a=1", '{"x":1}', 50
    )
    assert req.content == b'{"x":1}'


async def test_oauth2_mode_sends_the_grant_bearer(ovh_api, monkeypatch):
    seen = {}

    async def _grant(config, force_refresh=False):
        seen["url"], seen["id"], seen["refresh"] = config.token_url, config.client_id, force_refresh
        return "bearer-1"

    monkeypatch.setattr(_common, "get_grant_token", _grant)
    api = ovh_api({"/me": {"nichandle": "x"}})
    data, err = await OvhClient(parse_ovhcloud_token(OAUTH_TOKEN)).get("/me", "read the account")
    assert err is None and data == {"nichandle": "x"}
    assert api.requests[-1].headers["Authorization"] == "Bearer bearer-1"
    assert "X-Ovh-Signature" not in api.requests[-1].headers
    assert seen == {"url": "https://ca.ovh.com/auth/oauth2/token", "id": "client-id", "refresh": False}


async def test_oauth2_mode_refreshes_once_on_401(ovh_api, monkeypatch):
    calls = []

    async def _grant(config, force_refresh=False):
        calls.append(force_refresh)
        return "fresh" if force_refresh else "stale"

    monkeypatch.setattr(_common, "get_grant_token", _grant)
    api = ovh_api({"/me": {"nichandle": "x"}}, status={"/me": 401})
    _, err = await OvhClient(parse_ovhcloud_token(OAUTH_TOKEN)).get("/me", "read the account")
    assert calls == [False, True]
    assert err and err.startswith("Error: ")
    assert len(api.requests) == 2


async def test_errors_are_shaped(ovh_api):
    ovh_api({"/auth/time": 1, "/dedicated/server": {"message": "nope"}}, status={"/dedicated/server": 403})
    _, executor = _tools()
    out = await _call(executor, "list_ovhcloud_dedicated_servers")
    assert out.startswith("Error: ")


async def test_missing_credential_answers_before_any_request(ovh_api):
    api = ovh_api({})
    _, executor = _tools(build_ovhcloud_token("ovh-eu", "application_key", "AK", "", "AS"))
    out = await _call(executor, "list_ovhcloud_vps")
    assert out.startswith("Error: ") and "consumer key" in out
    assert api.requests == []


# --- tools -------------------------------------------------------------------


async def test_dedicated_servers_are_paginated(ovh_api):
    names = [f"ns{i}" for i in range(30)]
    routes = {"/auth/time": 1, "/dedicated/server": names}
    routes.update({f"/dedicated/server/{n}": {"name": n, "state": "ok"} for n in names})
    api = ovh_api(routes)
    _, executor = _tools()
    out = await _call(executor, "list_ovhcloud_dedicated_servers", limit=10, offset=20)
    assert out.startswith("30 dedicated server(s) (10 shown from offset 20)")
    assert '"ns20"' in out and '"ns19"' not in out
    detail_reads = [r for r in api.requests if r.url.path.startswith("/1.0/dedicated/server/ns")]
    assert len(detail_reads) == 10


async def test_server_detail_reads_ips_and_service(ovh_api):
    ovh_api(
        {
            "/auth/time": 1,
            "/dedicated/server/ns1": {"name": "ns1", "datacenter": "rbx8"},
            "/dedicated/server/ns1/ips": ["203.0.113.4/32"],
            "/dedicated/server/ns1/serviceInfos": {"expiration": "2027-01-01", "renew": {"automatic": True}},
        }
    )
    _, executor = _tools()
    out = await _call(executor, "get_ovhcloud_dedicated_server", service_name="ns1")
    payload = json.loads(out.split("\n", 1)[1])
    assert payload["ips"] == ["203.0.113.4/32"]
    assert payload["service"]["expiration"] == "2027-01-01"


async def test_ip_block_path_segment_is_encoded(ovh_api):
    api = ovh_api({"/auth/time": 1, "/ip/203.0.113.0/28": {"ip": "203.0.113.0/28"}})
    _, executor = _tools()
    await _call(executor, "get_ovhcloud_ip", ip_block="203.0.113.0/28")
    assert any(r.url.raw_path.startswith(b"/1.0/ip/203.0.113.0%2F28") for r in api.requests)


async def test_generic_get_refuses_credentials_and_other_hosts(ovh_api):
    api = ovh_api({"/auth/time": 1, "/me": {"nichandle": "x"}})
    _, executor = _tools()
    assert (await _call(executor, "get_ovhcloud_api_resource", path="https://evil.example/x")).startswith("Error: ")
    assert (await _call(executor, "get_ovhcloud_api_resource", path="/me/api/credential")).startswith("Error: ")
    assert (await _call(executor, "get_ovhcloud_api_resource", path="../../x")).startswith("Error: ")
    assert api.requests == []
    out = await _call(executor, "get_ovhcloud_api_resource", path="/1.0/me")
    assert out.startswith("GET /me")
    assert all(r.method == "GET" for r in api.requests)


def test_normalize_api_path():
    assert normalize_api_path("dedicated/server") == "/dedicated/server"
    assert normalize_api_path("/1.0/vps?x=1") == "/vps"
    assert normalize_api_path("") is None


async def test_inventory_normalizes_every_product(ovh_api):
    ovh_api(
        {
            "/auth/time": 1,
            "/dedicated/server": ["ns1"],
            "/dedicated/server/ns1": {
                "name": "ns1",
                "os": "debian12_64",
                "datacenter": "gra3",
                "state": "ok",
                "ip": "203.0.113.4",
                "reverse": "srv.example.com",
            },
            "/dedicated/server/ns1/ips": ["203.0.113.4/32", "10.0.0.0/24"],
            "/dedicated/server/ns1/tags": {"env": "prod"},
            "/cloud/project": ["p1"],
            "/cloud/project/p1/instance": [
                {
                    "id": "i1",
                    "name": "web",
                    "status": "ACTIVE",
                    "region": "GRA11",
                    "image": {"name": "Ubuntu 24.04"},
                    "ipAddresses": [{"ip": "198.51.100.9", "type": "public"}, {"ip": "10.1.0.5", "type": "private"}],
                }
            ],
            "/vps": ["vps-1"],
            "/vps/vps-1": {"name": "vps-1", "zone": "Region OpenStack: os-sbg2", "state": "running"},
            "/vps/vps-1/ips": ["192.0.2.10"],
            "/vps/vps-1/distribution": {"name": "Debian 12", "bitFormat": 64},
            "/dedicatedCloud": [],
        }
    )
    _, executor = _tools()
    out = await _call(executor, "list_ovhcloud_compute_inventory")
    payload = json.loads(out.split("\n", 1)[1])
    by_type = {row["type"]: row for row in payload["assets"]}
    assert set(by_type) == {"dedicated_server", "cloud_instance", "vps"}
    assert by_type["dedicated_server"]["public_ips"] == ["203.0.113.4"]
    assert by_type["dedicated_server"]["private_ips"] == ["10.0.0.0"]
    assert by_type["dedicated_server"]["tags"] == {"env": "prod"}
    assert by_type["cloud_instance"]["project"] == "p1"
    assert by_type["cloud_instance"]["private_ips"] == ["10.1.0.5"]
    assert by_type["vps"]["arch"] == "x86_64"
    filtered = await _call(executor, "list_ovhcloud_compute_inventory", types=["cloud_instance"], region="gra")
    assert json.loads(filtered.split("\n", 1)[1])["assets"][0]["id"] == "i1"


def test_inventory_rows_share_one_shape():
    keys = set(dedicated_row({"name": "a"}, [], None))
    assert keys == set(cloud_row({"id": "b"}, "p"))
    assert filter_rows([{"region": "GRA11", "tags": {"env": "prod"}}], region="gra", tag="env=prod")
    assert not filter_rows([{"region": "GRA11", "tags": {}}], tag="env")


# --- safety ------------------------------------------------------------------


def test_secrets_are_never_answered():
    data = {"name": "db", "password": "p", "nested": [{"privateKey": "k", "consumer_key": "c"}]}
    assert redact(data) == {
        "name": "db",
        "password": "[redacted]",
        "nested": [{"privateKey": "[redacted]", "consumer_key": "[redacted]"}],
    }
    out = render("t", data)
    assert 'p"' not in out.split("\n", 1)[1].replace("[redacted]", "")


def test_answers_are_bounded():
    out = render("t", ["x" * 100] * 1000)
    assert len(out) < _common.MAX_ANSWER_CHARS + 300
    assert "narrow the call" in out


def test_write_tools_are_classified_as_writes_and_reads_as_reads():
    defs, _ = _tools()
    names = {d.name for d in defs}
    assert WRITE_TOOLS.issubset(names)
    for name in names:
        expected = "write" if name in WRITE_TOOLS else "read"
        assert infer_tool_type(name) == expected, name


def test_read_only_binding_withholds_the_write_tools():
    from app.services.integration_read_only import withhold_write_tools

    defs, executor = _tools()
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="OVHcloud")}
    assert not kept & WRITE_TOOLS
    assert "list_ovhcloud_compute_inventory" in kept


def test_no_destructive_tool_exists():
    defs, _ = _tools()
    for d in defs:
        assert not any(w in d.name for w in ("delete", "order", "reinstall", "create", "terminate")), d.name


def test_provider_is_in_the_catalog_and_the_tool_registry():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS

    entry = PROVIDER_CATALOG["ovhcloud"]
    assert entry["logo"] == "/logos/ovhcloud.svg"
    assert PROVIDER_TOOLS["ovhcloud"] is ovhcloud.build_tools
    names = [d.name for d in PROVIDER_TOOLS["ovhcloud"](build_dummy_token("ovhcloud"), CallableToolExecutor())]
    assert "list_ovhcloud_dedicated_servers" in names


async def test_test_connection_both_modes(ovh_api, monkeypatch):
    ovh_api(
        {
            "/auth/time": 1,
            "/me": {"nichandle": "ab123-ovh"},
            "/auth/currentCredential": {"rules": [{"method": "GET", "path": "/*"}]},
        }
    )
    ok, message = await ovhcloud.validate_connection(KEY_TOKEN)
    assert ok and "ab123-ovh" in message and "GET /*" in message
    assert "AS" not in message.split("ab123-ovh")[0].replace("OVHcloud", "")

    async def _grant(config, force_refresh=False):
        return "b"

    monkeypatch.setattr(_common, "get_grant_token", _grant)
    ok, message = await ovhcloud.validate_connection(OAUTH_TOKEN)
    assert ok and "OAuth2" in message

    ok, message = await ovhcloud.validate_connection(build_ovhcloud_token("ovh-eu", "oauth2", "", "", ""))
    assert not ok and "client ID" in message
