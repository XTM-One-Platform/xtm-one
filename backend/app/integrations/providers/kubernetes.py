# SPDX-License-Identifier: Apache-2.0

"""Kubernetes integration - a read-only view of a cluster's network surface.

Through the Kubernetes API server (core API ``/api/v1``):

  - Services: every service of the cluster or of one namespace with its type,
    cluster IP, ports, node ports, external IPs and load balancer ingress
    (the ``kubectl get svc -A`` read).
  - Namespaces: their names and phase.
  - Nodes: their addresses (internal and external IPs, host names), so a node
    port can be turned into a reachable address.

Read-only by construction: every tool is a GET and its name starts with
``list_``. Authentication is a service account token sent as
``Authorization: Bearer <token>``; grant it ``get`` / ``list`` on ``services``,
``namespaces`` and ``nodes`` (a ClusterRole), nothing more.

The API server URL is the integration's ``base_url``, required and checked by
:func:`~app.integrations.http.operator_base_url`. A cluster CA that is not
publicly signed (the usual case) is pasted as ``ca_cert`` (PEM): the server's
certificate is then verified against it, never skipped.

Token format: ``base_url||ca_segment||token``, the CA as
:func:`encode_ca_segment` writes it (base64url, so a PEM never holds ``||``).
"""

from __future__ import annotations

import base64
import binascii
import re
import ssl
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.connectivity import ProbeContext, ProbeResult, register_connectivity
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    bearer_auth_headers,
    clip_text,
    operator_base_url,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

PROVIDER = "kubernetes"
_PROVIDER = "Kubernetes"
_SOURCE = "integration:kubernetes"

_TIMEOUT = 30
_PAGE_LIMIT = 500
_MAX_PAGES = 20
_MAX_LINES = 400
_CA_PREFIX = "ca1."
_PEM_MARK = "-----BEGIN CERTIFICATE-----"

#: A namespace name (RFC 1123 label).
_NAMESPACE_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_SERVICE_TYPES = ("ClusterIP", "NodePort", "LoadBalancer", "ExternalName")


def kubernetes_api(base_url: str) -> str | None:
    """The API server URL, ``None`` when *base_url* is blank or not usable."""
    return operator_base_url(base_url) if (base_url or "").strip() else None


def build_kubernetes_headers(token: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity probe."""
    return bearer_auth_headers(token, extra={"Accept": "application/json"})


def encode_ca_segment(ca_cert: str | None) -> str:
    """The token segment for a pasted CA certificate, ``""`` when none is set."""
    pem = (ca_cert or "").strip()
    if not pem:
        return ""
    return _CA_PREFIX + base64.urlsafe_b64encode(pem.encode()).decode()


def decode_ca_segment(segment: str) -> str:
    """The PEM :func:`encode_ca_segment` wrote, ``""`` when the segment is empty or unreadable."""
    text = (segment or "").strip()
    if not text.startswith(_CA_PREFIX):
        return ""
    try:
        return base64.urlsafe_b64decode(text[len(_CA_PREFIX) :].encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""


def tls_verify(ca_pem: str) -> ssl.SSLContext | bool:
    """The ``verify`` option: the pasted CA when there is one, the public roots otherwise.

    Raises :class:`ValueError` when a pasted CA is not a PEM certificate.
    """
    if not ca_pem:
        return True
    if _PEM_MARK not in ca_pem:
        raise ValueError("the CA certificate is not a PEM certificate (-----BEGIN CERTIFICATE-----).")
    try:
        return ssl.create_default_context(cadata=ca_pem)
    except ssl.SSLError as exc:
        raise ValueError(f"the CA certificate could not be loaded ({exc.reason or exc}).") from None


def _clean(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _port_text(port: dict[str, Any]) -> str:
    number = port.get("port")
    target = port.get("targetPort")
    text = f"{number}/{port.get('protocol') or 'TCP'}"
    if port.get("nodePort"):
        text += f" nodePort {port['nodePort']}"
    if target not in (None, "", number):
        text += f" -> {target}"
    if port.get("name"):
        text = f"{_clean(port['name'], 40)} {text}"
    return text


def service_line(item: dict[str, Any]) -> str:
    """One service as ``kubectl get svc -o wide`` would show it, on one line."""
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    ingress = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    kind = spec.get("type") or "ClusterIP"
    parts = [f"- {_clean(meta.get('namespace'), 63)}/{_clean(meta.get('name'), 63)}: {kind}"]
    if kind == "ExternalName":
        parts.append(f"external name {_clean(spec.get('externalName'))}")
    else:
        parts.append(f"cluster IP {_clean(spec.get('clusterIP'), 64)}")
    ports = [_port_text(p) for p in spec.get("ports") or [] if isinstance(p, dict)]
    if ports:
        parts.append("ports " + ", ".join(ports))
    external = [str(ip) for ip in spec.get("externalIPs") or []]
    external += [
        str(i.get("ip") or i.get("hostname"))
        for i in ingress
        if isinstance(i, dict) and (i.get("ip") or i.get("hostname"))
    ]
    if external:
        parts.append("external " + ", ".join(_clean(x, 253) for x in external))
    return ", ".join(parts)


def _node_line(item: dict[str, Any]) -> str:
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    addresses = [
        f"{a.get('type')} {a.get('address')}"
        for a in status.get("addresses") or []
        if isinstance(a, dict) and a.get("address")
    ]
    ready = next(
        (c.get("status") for c in status.get("conditions") or [] if isinstance(c, dict) and c.get("type") == "Ready"),
        "Unknown",
    )
    line = f"- {_clean(meta.get('name'), 253)}: ready {ready}"
    if (item.get("spec") or {}).get("unschedulable"):
        line += ", cordoned"
    return line + (", " + ", ".join(_clean(a, 260) for a in addresses) if addresses else "")


def _items(resp: httpx.Response) -> tuple[list[dict[str, Any]], str]:
    try:
        body = resp.json()
    except ValueError:
        return [], ""
    if not isinstance(body, dict):
        return [], ""
    items = [i for i in body.get("items") or [] if isinstance(i, dict)]
    return items, str((body.get("metadata") or {}).get("continue") or "")


def _bounded(lines: list[str], what: str, filters: str = "") -> str:
    if not lines:
        return f"No {what}{filters}."
    shown = lines[:_MAX_LINES]
    head = (
        f"{len(lines)} {what}{filters}" + (f", the first {_MAX_LINES} shown" if len(lines) > _MAX_LINES else "") + ":"
    )
    return "\n".join([head, *shown])


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    base_url, ca_segment, access_token = parse_compound_token(token or "", 3, greedy_last=True)
    api = kubernetes_api(base_url)
    if api is None or not access_token.strip():
        return []
    headers = build_kubernetes_headers(access_token.strip())
    ca_pem = decode_ca_segment(ca_segment)

    async def _list(
        path: str, action: str, params: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Every item of a list endpoint, page by page (``limit`` / ``continue``)."""
        try:
            verify = tls_verify(ca_pem)
        except ValueError as exc:
            return [], f"Error: {exc} Fix ca_cert on the Kubernetes integration."
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {**(params or {}), "limit": _PAGE_LIMIT}
        async with egress_client(timeout=_TIMEOUT, verify=verify) as client:
            for _ in range(_MAX_PAGES):
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{api}{path}",
                    _PROVIDER,
                    action,
                    headers=headers,
                    params=query,
                    retry_statuses=TRANSIENT_RETRY_STATUSES,
                )
                if err:
                    return items, err
                page, cont = _items(resp)
                items.extend(page)
                if not cont:
                    break
                query["continue"] = cont
        return items, None

    async def list_kubernetes_services(namespace: str = "", service_type: str = "") -> str:
        ns = (namespace or "").strip()
        if ns and not _NAMESPACE_RE.fullmatch(ns):
            return "Error: namespace must be a Kubernetes namespace name (lower-case letters, digits and '-')."
        kind = (service_type or "").strip()
        if kind and kind not in _SERVICE_TYPES:
            return f"Error: service_type must be one of {', '.join(_SERVICE_TYPES)}."
        path = f"/api/v1/namespaces/{ns}/services" if ns else "/api/v1/services"
        items, err = await _list(path, "list services")
        if err:
            return err
        if kind:
            items = [i for i in items if ((i.get("spec") or {}).get("type") or "ClusterIP") == kind]
        filters = (f" in namespace {ns}" if ns else "") + (f" of type {kind}" if kind else "")
        return _bounded([service_line(i) for i in items], "service(s)", filters)

    async def list_kubernetes_namespaces() -> str:
        items, err = await _list("/api/v1/namespaces", "list namespaces")
        if err:
            return err
        lines = [
            f"- {_clean((i.get('metadata') or {}).get('name'), 63)}: {_clean((i.get('status') or {}).get('phase'), 20)}"
            for i in items
        ]
        return _bounded(lines, "namespace(s)")

    async def list_kubernetes_nodes() -> str:
        items, err = await _list("/api/v1/nodes", "list nodes")
        if err:
            return err
        return _bounded([_node_line(i) for i in items], "node(s)")

    handlers = {
        "list_kubernetes_services": list_kubernetes_services,
        "list_kubernetes_namespaces": list_kubernetes_namespaces,
        "list_kubernetes_nodes": list_kubernetes_nodes,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "list_kubernetes_services",
            "List a Kubernetes cluster's services (read-only, the 'kubectl get svc -A' read): namespace/name, "
            "type, cluster IP, ports with their node ports and target ports, external IPs and load balancer "
            "ingress. Pair a node port with list_kubernetes_nodes for the address it is reachable on.",
            {
                "namespace": {"type": "string", "description": "Only this namespace (default: every namespace)"},
                "service_type": {
                    "type": "string",
                    "enum": list(_SERVICE_TYPES),
                    "description": "Only services of this type (e.g. NodePort, LoadBalancer)",
                },
            },
            [],
        ),
        _tool(
            "list_kubernetes_namespaces", "List a Kubernetes cluster's namespaces and their phase (read-only).", {}, []
        ),
        _tool(
            "list_kubernetes_nodes",
            "List a Kubernetes cluster's nodes (read-only): readiness, cordoned or not, and their addresses "
            "(InternalIP, ExternalIP, Hostname).",
            {},
            [],
        ),
    ]


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """List one namespace with the integration's token, its CA verifying the server."""
    api = kubernetes_api(str(ctx.extra.get("base_url") or ""))
    if api is None:
        return ProbeResult.failed(
            "Missing or invalid base_url (the API server URL, e.g. https://k8s.company.com:6443)."
        )
    token = (ctx.token or "").strip()
    if not token:
        return ProbeResult.failed("Missing service account token.")
    try:
        verify = tls_verify(str(ctx.extra.get("ca_cert") or "").strip())
    except ValueError as exc:
        return ProbeResult.failed(f"ca_cert: {exc}")
    async with egress_client(timeout=15, verify=verify) as client:
        resp, err = await safe_request(
            client,
            "get",
            f"{api}/api/v1/namespaces",
            _PROVIDER,
            "list namespaces",
            headers=build_kubernetes_headers(token),
            params={"limit": 1},
        )
    if resp is None or err:
        return ProbeResult.failed(
            (err or "The Kubernetes API server could not be reached.")
            + " The token's service account needs get/list on services, namespaces and nodes."
        )
    return ProbeResult.ok("Connected: the API server accepted the token and listed namespaces.")


register_connectivity(PROVIDER, check_connectivity)
