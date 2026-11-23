# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A minimal client of the Kubernetes API server for the agentic coding labs (``AGENTIC_CODING``).

The labs (:mod:`labs`) create, read, list and delete pods in ONE namespace,
read a container's log and run a command in a container. That is six calls,
so the platform talks to the API server with ``httpx`` and a service-account
token rather than depending on the Kubernetes client library; a command runs
over the API server's exec WebSocket (``v4.channel.k8s.io``) with the
``websockets`` package the ASGI server already ships.

Where the API server is: ``AGENTIC_LABS_API_URL`` when set, else the in-cluster
address (``KUBERNETES_SERVICE_HOST`` / ``KUBERNETES_SERVICE_PORT``). The token
and the CA are read from ``AGENTIC_LABS_TOKEN_PATH`` / ``AGENTIC_LABS_CA_PATH``
(the pod's mounted service account by default) on every call, so a rotated
token is picked up. The token never reaches an agent, a log line or a result.
"""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

#: Bytes of stdin sent per WebSocket frame.
_STDIN_CHUNK = 512 * 1024
#: Channels of the ``v4.channel.k8s.io`` exec protocol.
_STDIN, _STDOUT, _STDERR, _STATUS = 0, 1, 2, 3


class KubeError(RuntimeError):
    """A call the API server refused or could not answer; ``status`` is its HTTP status (0 when none).

    ``handshake`` is the HTTP status of an exec stream refused at its upgrade (0 when none): kept apart
    from ``status`` so a transient 404 on the upgrade never reads as a REST 404 (the pod is gone).
    """

    def __init__(self, message: str, status: int = 0, *, handshake: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.handshake = handshake


@dataclass
class ExecResult:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""
    message: str = ""


@dataclass(frozen=True)
class KubeConfig:
    api_url: str
    namespace: str
    token_path: str
    ca_path: str
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, namespace: str | None = None) -> KubeConfig:
        """The labs' API server and credentials, in *namespace* when given, else ``AGENTIC_LABS_NAMESPACE``."""
        from app.core.config import settings

        url = (settings.agentic_labs_api_url or "").strip().rstrip("/")
        if not url:
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                raise KubeError("no Kubernetes API server: set AGENTIC_LABS_API_URL or run the platform in a cluster")
            url = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
        return cls(
            api_url=url,
            namespace=(settings.agentic_labs_namespace if namespace is None else namespace).strip(),
            token_path=settings.agentic_labs_token_path,
            ca_path=settings.agentic_labs_ca_path,
        )

    def token(self) -> str:
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise KubeError(f"the service-account token could not be read at {self.token_path}: {exc.strerror}") from exc

    def ssl_context(self) -> ssl.SSLContext | bool:
        if self.api_url.startswith("http://"):
            return False
        context = ssl.create_default_context()
        if self.ca_path and os.path.exists(self.ca_path):
            context.load_verify_locations(self.ca_path)
        return context


def _transport_failure(method: str, path: str, exc: Exception) -> str:
    """A readable text for a call that got no answer: an httpx timeout's own text is often empty."""
    detail = str(exc).strip()
    return f"{method} {path}: no answer from the API server ({type(exc).__name__}{': ' + detail if detail else ''})"


def _reason(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:300]
    return str(body.get("message") or body.get("reason") or body)[:300]


class KubeClient:
    """The pod calls the labs make, in the configured namespace only."""

    def __init__(self, config: KubeConfig | None = None, *, transport: Any = None) -> None:
        self.config = config or KubeConfig.from_settings()
        self._transport = transport

    def _pods(self, name: str = "", sub: str = "") -> str:
        path = f"/api/v1/namespaces/{quote(self.config.namespace, safe='')}/pods"
        if name:
            path += f"/{quote(name, safe='')}"
        return path + (f"/{sub}" if sub else "")

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.config.token()}", "Accept": "application/json"}
        async with httpx.AsyncClient(
            base_url=self.config.api_url,
            verify=self.config.ssl_context(),
            timeout=30.0,
            trust_env=False,
            transport=self._transport,
        ) as client:
            try:
                response = await client.request(method, path, params=params, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise KubeError(_transport_failure(method, path, exc)) from exc
        if response.status_code >= 400:
            raise KubeError(f"{method} {path}: {response.status_code} {_reason(response)}", response.status_code)
        return response

    async def create_pod(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("POST", self._pods(), body=manifest)).json()

    async def get_pod(self, name: str) -> dict[str, Any] | None:
        try:
            return (await self._request("GET", self._pods(name))).json()
        except KubeError as exc:
            if exc.status == 404:
                return None
            raise

    async def list_pods(self, label_selector: str) -> list[dict[str, Any]]:
        response = await self._request("GET", self._pods(), params={"labelSelector": label_selector})
        return list(response.json().get("items") or [])

    async def patch_annotations(self, name: str, annotations: dict[str, str]) -> None:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.config.token()}",
            "Content-Type": "application/merge-patch+json",
        }
        async with httpx.AsyncClient(
            base_url=self.config.api_url, verify=self.config.ssl_context(), timeout=30.0, trust_env=False, transport=self._transport
        ) as client:
            try:
                response = await client.patch(
                    self._pods(name), content=json.dumps({"metadata": {"annotations": annotations}}), headers=headers
                )
            except httpx.HTTPError as exc:
                raise KubeError(_transport_failure("PATCH", self._pods(name), exc)) from exc
        if response.status_code >= 400:
            raise KubeError(f"PATCH {self._pods(name)}: {response.status_code} {_reason(response)}", response.status_code)

    async def delete_pod(self, name: str) -> bool:
        """Delete pod *name*; ``False`` when it was already gone."""
        try:
            await self._request("DELETE", self._pods(name), params={"gracePeriodSeconds": 5})
        except KubeError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def _custom(self, group: str, version: str, plural: str, name: str = "") -> str:
        path = f"/apis/{quote(group, safe='')}/{quote(version, safe='')}/namespaces/{quote(self.config.namespace, safe='')}/{quote(plural, safe='')}"
        return path + (f"/{quote(name, safe='')}" if name else "")

    async def list_custom(self, group: str, version: str, plural: str) -> list[dict[str, Any]]:
        """Every object of a custom resource (``plural`` of ``group/version``) in the namespace."""
        response = await self._request("GET", self._custom(group, version, plural))
        return list(response.json().get("items") or [])

    async def get_custom(self, group: str, version: str, plural: str, name: str) -> dict[str, Any] | None:
        """One custom object; ``None`` when it does not exist."""
        try:
            return (await self._request("GET", self._custom(group, version, plural, name))).json()
        except KubeError as exc:
            if exc.status == 404:
                return None
            raise

    async def delete_custom(self, group: str, version: str, plural: str, name: str) -> bool:
        """Delete one custom object; ``False`` when it was already gone."""
        try:
            await self._request("DELETE", self._custom(group, version, plural, name))
        except KubeError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def logs(self, name: str, container: str, *, tail_lines: int, limit_bytes: int) -> str:
        response = await self._request(
            "GET", self._pods(name, "log"), params={"container": container, "tailLines": tail_lines, "limitBytes": limit_bytes}
        )
        return response.text

    def exec_url(self, name: str, container: str, command: list[str], *, stdin: bool) -> str:
        query = [("container", container), ("stdout", "true"), ("stderr", "true"), ("stdin", "true" if stdin else "false")]
        query += [("command", part) for part in command]
        base = self.config.api_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}{self._pods(name, 'exec')}?{urlencode(query)}"

    async def exec(self, name: str, container: str, command: list[str], *, stdin: bytes | None = None, timeout: float = 600.0) -> ExecResult:
        """Run *command* in *container* of pod *name*, *stdin* written first; its exit code and output."""
        import asyncio

        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
            from websockets.typing import Subprotocol
        except ImportError as exc:  # shipped with uvicorn[standard]; say so rather than fail obscurely
            raise KubeError("running a command in a lab needs the 'websockets' package") from exc

        url = self.exec_url(name, container, command, stdin=stdin is not None)
        context = self.config.ssl_context()
        out, err = bytearray(), bytearray()
        status: dict[str, Any] = {}
        cut: list[Exception] = []

        async def _session() -> None:
            async with connect(
                url,
                additional_headers={"Authorization": f"Bearer {self.config.token()}"},
                subprotocols=[Subprotocol("v4.channel.k8s.io")],
                ssl=context if isinstance(context, ssl.SSLContext) else None,
                max_size=None,
            ) as ws:
                if stdin is not None:
                    try:
                        for start in range(0, len(stdin), _STDIN_CHUNK):
                            await ws.send(bytes([_STDIN]) + stdin[start : start + _STDIN_CHUNK])
                    except ConnectionClosed as exc:
                        # The command may end before it reads all of stdin (tar stops at its end-of-archive
                        # blocks, before the record padding): the server then closes the stream while the
                        # last chunk is sent. The frames it sent first (output, status) are still read below.
                        cut.append(exc)
                async for frame in ws:
                    data = frame if isinstance(frame, bytes) else frame.encode("utf-8")
                    if not data:
                        continue
                    channel, payload = data[0], data[1:]
                    if channel == _STDOUT:
                        out.extend(payload)
                    elif channel == _STDERR:
                        err.extend(payload)
                    elif channel == _STATUS and payload:
                        status.update(json.loads(payload))
            if cut and not status:
                # Closed while stdin was sent and no status came: the command's end is unknown.
                raise cut[0]

        try:
            await asyncio.wait_for(_session(), timeout)
        except TimeoutError as exc:
            raise KubeError(f"the command did not finish within {timeout:g} s") from exc
        except InvalidStatus as exc:
            # The API server refused the upgrade: a 404 while it has not caught up with a new pod, a 5xx.
            # The code goes in ``handshake``, never ``status``: callers read status 404 as a pod gone.
            code = int(getattr(exc.response, "status_code", 0) or 0)
            raise KubeError(f"the API server refused the exec stream (HTTP {code})", handshake=code) from exc
        except (InvalidHandshake, ConnectionClosed, OSError) as exc:
            if status:
                # The status frame arrived before the stream closed: the command ended, its answer stands.
                return ExecResult(
                    exit_code=exit_code_of(status),
                    stdout=bytes(out),
                    stderr=bytes(err),
                    message=str(status.get("message") or ""),
                )
            # A stream lost before the status frame: as retryable as a stall, never an unknown type.
            detail = str(exc).strip()
            raise KubeError(
                f"the exec stream was lost ({type(exc).__name__}{': ' + detail if detail else ''})"
            ) from exc
        return ExecResult(
            exit_code=exit_code_of(status),
            stdout=bytes(out),
            stderr=bytes(err),
            message=str(status.get("message") or ""),
        )


def exit_code_of(status: dict[str, Any]) -> int:
    """The exit code an exec status object carries: 0 on ``Success``, the ``ExitCode`` cause otherwise (1 when none)."""
    if not status or status.get("status") == "Success":
        return 0
    for cause in (status.get("details") or {}).get("causes") or []:
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message", 1))
            except (TypeError, ValueError):
                return 1
    return 1
