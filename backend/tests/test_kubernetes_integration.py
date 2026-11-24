"""The read-only Kubernetes integration provider.

Covers the catalog entry and registration, the ``base_url||ca_segment||token``
credential (the CA rides base64url-encoded), that every tool is a read, the
service listing against a mocked API server (``httpx.MockTransport``):
cluster-wide and per namespace, type filter, node ports, external IPs, paging
through ``continue``; the namespaces and nodes reads, the CA refusal and the
connectivity probe.
"""

from __future__ import annotations

import ssl

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.connectivity import ProbeContext, has_connectivity_probe
from app.integrations.providers import kubernetes
from app.integrations.providers.kubernetes import (
    build_tools,
    check_connectivity,
    decode_ca_segment,
    encode_ca_segment,
    service_line,
    tls_verify,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_API = "https://k8s.example.com:6443"
_TOKEN = f"{_API}||||saTok"

_NODEPORT = {
    "metadata": {"namespace": "web", "name": "front"},
    "spec": {
        "type": "NodePort",
        "clusterIP": "10.0.0.12",
        "ports": [{"name": "http", "port": 80, "targetPort": 8080, "nodePort": 30080, "protocol": "TCP"}],
    },
}
_LB = {
    "metadata": {"namespace": "edge", "name": "gw"},
    "spec": {"type": "LoadBalancer", "clusterIP": "10.0.0.20", "ports": [{"port": 443, "nodePort": 31443}]},
    "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.7"}]}},
}
_CLUSTER = {
    "metadata": {"namespace": "default", "name": "kubernetes"},
    "spec": {"clusterIP": "10.0.0.1", "ports": [{"port": 443, "targetPort": 6443}]},
}


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _mock(monkeypatch, responder) -> tuple[list[httpx.Request], list[dict]]:
    seen: list[httpx.Request] = []
    options: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def _client(**kwargs):
        options.append(dict(kwargs))
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(kubernetes, "egress_client", _client)
    return seen, options


def _list(items, cont: str = ""):
    return {"kind": "List", "metadata": {"continue": cont} if cont else {}, "items": items}


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["kubernetes"]
    assert entry["auth_type"] == "api_key"
    assert set(entry["extra_fields"]) == {"base_url", "ca_cert"}
    assert entry["extra_field_required_when"] == {"base_url": {}}
    assert PROVIDER_TOOLS["kubernetes"] is build_tools
    assert has_connectivity_probe("kubernetes")


def test_dummy_token_builds_every_tool():
    names = [t.name for t in build_tools(build_dummy_token("kubernetes"), _SafeIntegrationExecutor())]
    assert names == ["list_kubernetes_services", "list_kubernetes_namespaces", "list_kubernetes_nodes"]


def test_every_tool_is_a_read():
    _, tools = _build()
    assert {integration_tool_access(t.name) for t in tools} == {"read"}


@pytest.mark.parametrize("token", ["", "||||saTok", f"{_API}||||", "ftp://x||||saTok", "https://a:b@x.com||||saTok"])
def test_unusable_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_ca_segment_round_trips_and_never_holds_the_delimiter():
    pem = "-----BEGIN CERTIFICATE-----\nMIIB||x\n-----END CERTIFICATE-----"
    segment = encode_ca_segment(pem)
    assert "||" not in segment
    assert decode_ca_segment(segment) == pem
    assert encode_ca_segment("  ") == ""
    assert decode_ca_segment("") == ""
    assert decode_ca_segment("ca1.%%%") == ""


def test_tls_verify_never_skips_verification():
    assert tls_verify("") is True
    with pytest.raises(ValueError, match="not a PEM certificate"):
        tls_verify("not a cert")
    with pytest.raises(ValueError, match="could not be loaded"):
        tls_verify("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----")


def test_service_line_shows_ports_node_ports_and_external_addresses():
    assert service_line(_NODEPORT) == (
        "- web/front: NodePort, cluster IP 10.0.0.12, ports http 80/TCP nodePort 30080 -> 8080"
    )
    assert service_line(_LB) == (
        "- edge/gw: LoadBalancer, cluster IP 10.0.0.20, ports 443/TCP nodePort 31443, external 203.0.113.7"
    )
    assert service_line(_CLUSTER) == "- default/kubernetes: ClusterIP, cluster IP 10.0.0.1, ports 443/TCP -> 6443"
    external_name = {
        "metadata": {"namespace": "a", "name": "db"},
        "spec": {"type": "ExternalName", "externalName": "db.example.com"},
    }
    assert service_line(external_name) == "- a/db: ExternalName, external name db.example.com"


@pytest.mark.asyncio
async def test_list_services_cluster_wide_follows_continue(monkeypatch):
    pages = iter([_list([_NODEPORT], cont="c1"), _list([_LB, _CLUSTER])])
    seen, options = _mock(monkeypatch, lambda _r: httpx.Response(200, json=next(pages)))
    handlers, _ = _build()
    out = await handlers["list_kubernetes_services"]()
    assert [str(r.url.copy_with(query=None)) for r in seen] == [f"{_API}/api/v1/services"] * 2
    assert seen[0].url.params["limit"] == "500"
    assert seen[1].url.params["continue"] == "c1"
    assert seen[0].headers["Authorization"] == "Bearer saTok"
    assert all(r.method == "GET" for r in seen)
    assert options[0]["verify"] is True
    assert out.splitlines()[0] == "3 service(s):"
    assert "- web/front: NodePort" in out and "external 203.0.113.7" in out


@pytest.mark.asyncio
async def test_list_services_of_a_namespace_and_a_type(monkeypatch):
    seen, _ = _mock(monkeypatch, lambda _r: httpx.Response(200, json=_list([_NODEPORT, _CLUSTER])))
    handlers, _ = _build()
    out = await handlers["list_kubernetes_services"](namespace="web", service_type="NodePort")
    assert seen[0].url.path == "/api/v1/namespaces/web/services"
    assert out.splitlines() == [
        "1 service(s) in namespace web of type NodePort:",
        "- web/front: NodePort, cluster IP 10.0.0.12, ports http 80/TCP nodePort 30080 -> 8080",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"namespace": "../x"}, "namespace must be"),
        ({"namespace": "Web"}, "namespace must be"),
        ({"service_type": "Headless"}, "service_type must be one of"),
    ],
)
async def test_bad_arguments_are_refused_before_any_call(monkeypatch, kwargs, message):
    seen, _ = _mock(monkeypatch, lambda _r: httpx.Response(200, json=_list([])))
    handlers, _ = _build()
    out = await handlers["list_kubernetes_services"](**kwargs)
    assert message in out
    assert seen == []


@pytest.mark.asyncio
async def test_a_pasted_ca_is_the_verify_context(monkeypatch):
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(kubernetes, "tls_verify", lambda pem: sentinel if pem == "PEM" else True)
    _, options = _mock(monkeypatch, lambda _r: httpx.Response(200, json=_list([])))
    handlers, _ = _build(f"{_API}||{encode_ca_segment('PEM')}||saTok")
    assert await handlers["list_kubernetes_services"]() == "No service(s)."
    assert options[0]["verify"] is sentinel


@pytest.mark.asyncio
async def test_an_unreadable_ca_is_said_and_nothing_is_called(monkeypatch):
    seen, _ = _mock(monkeypatch, lambda _r: httpx.Response(200, json=_list([])))
    handlers, _ = _build(f"{_API}||{encode_ca_segment('garbage')}||saTok")
    out = await handlers["list_kubernetes_nodes"]()
    assert out.startswith("Error: the CA certificate is not a PEM certificate")
    assert seen == []


@pytest.mark.asyncio
async def test_namespaces_and_nodes(monkeypatch):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/namespaces":
            return httpx.Response(200, json=_list([{"metadata": {"name": "web"}, "status": {"phase": "Active"}}]))
        node = {
            "metadata": {"name": "n1"},
            "spec": {"unschedulable": True},
            "status": {
                "addresses": [
                    {"type": "InternalIP", "address": "192.168.1.10"},
                    {"type": "ExternalIP", "address": "198.51.100.4"},
                ],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        return httpx.Response(200, json=_list([node]))

    _mock(monkeypatch, _respond)
    handlers, _ = _build()
    assert await handlers["list_kubernetes_namespaces"]() == "1 namespace(s):\n- web: Active"
    assert await handlers["list_kubernetes_nodes"]() == (
        "1 node(s):\n- n1: ready True, cordoned, InternalIP 192.168.1.10, ExternalIP 198.51.100.4"
    )


@pytest.mark.asyncio
async def test_a_refusal_is_said(monkeypatch):
    _mock(monkeypatch, lambda _r: httpx.Response(403, json={"message": "services is forbidden"}))
    handlers, _ = _build()
    out = await handlers["list_kubernetes_services"]()
    assert out.startswith("Error: Kubernetes access denied while trying to list services")
    assert "services is forbidden" in out


def _ctx(token: str | None, extra: dict) -> ProbeContext:
    return ProbeContext(provider="kubernetes", name="Kubernetes", token=token, extra=extra)


@pytest.mark.asyncio
async def test_probe(monkeypatch):
    seen, _ = _mock(monkeypatch, lambda _r: httpx.Response(200, json=_list([{"metadata": {"name": "default"}}])))
    ok = await check_connectivity(_ctx("saTok", {"base_url": _API}))
    assert ok.status == "ok"
    assert seen[0].url.path == "/api/v1/namespaces"
    assert seen[0].url.params["limit"] == "1"
    assert (await check_connectivity(_ctx("saTok", {}))).status == "error"
    assert (await check_connectivity(_ctx("", {"base_url": _API}))).status == "error"
    bad_ca = await check_connectivity(_ctx("saTok", {"base_url": _API, "ca_cert": "nope"}))
    assert bad_ca.status == "error" and "ca_cert" in bad_ca.message


@pytest.mark.asyncio
async def test_probe_names_the_grants_on_a_refusal(monkeypatch):
    _mock(monkeypatch, lambda _r: httpx.Response(403, json={}))
    result = await check_connectivity(_ctx("saTok", {"base_url": _API}))
    assert result.status == "error"
    assert "get/list on services, namespaces and nodes" in result.message
