"""The Defender investigation package reaches the agent.

Maintainer decision: the agent gets the time-limited SAS link AND the
ZIP stored as a file of the conversation (or run), within the download cap,
with the access rules of every other conversation file.

The tool runs for real against a fake Defender for Endpoint API served through
``httpx.MockTransport`` (documented response shapes, no network, no token).
"""

from __future__ import annotations

import io
import ipaddress
import zipfile
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations.providers import defender
from app.llm.base import FileRef
from app.services import integration_tools, web_fetch
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

MDE = "api.securitycenter.microsoft.com"
SAS_HOST = "api.security.microsoft.com"
SAS = f"https://{SAS_HOST}:443/safedownload/WDATP_Investigation_Package.zip?token=gbDyj7y%2fbW"


class FakeDefender:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []

    def on(self, host: str, path: str, response: Any) -> None:
        self.routes.append((host, path, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for host, path, response in self.routes:
            if request.url.host == host and request.url.path == path:
                return response(request) if callable(response) else response
        raise AssertionError(f"no fake route for {request.method} {request.url}")


@pytest.fixture
def api(monkeypatch) -> FakeDefender:
    fake = FakeDefender()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    real_policy = web_fetch.validate_outbound_url

    def _policy(url: str, *, allow_private: bool = False) -> str:
        # An IP literal is judged by the real policy; a named host of the fake
        # stands for a public one (no DNS in a unit test).
        try:
            ipaddress.ip_address(httpx.URL(url).host)
        except ValueError:
            return url
        return real_policy(url, allow_private=allow_private)

    monkeypatch.setattr(web_fetch, "validate_outbound_url", _policy)
    return fake


class Persisted:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []

    async def __call__(self, filename: str, data: bytes, mime: str) -> FileRef:
        self.calls.append((filename, data, mime))
        return FileRef.from_bytes(data=data, filename=filename, content_type=mime)


def _executor(**kwargs: Any) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    defender.build_tools("", executor, token_acquirer=AsyncMock(return_value="tok"), **kwargs)
    return executor


async def _call(executor: CallableToolExecutor, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(
        ToolCall(id="c1", name="get_defender_investigation_package", arguments=arguments)
    )


def _package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Processes/processes.csv", "pid,name\n4,System\n")
        archive.writestr("Autoruns/Run.txt", "HKLM\\...\\Run")
    return buffer.getvalue()


def _action(status: str = "Succeeded", kind: str = "CollectInvestigationPackage") -> dict[str, Any]:
    return {"id": "A1", "type": kind, "status": status, "computerDnsName": "desktop-01.contoso.com"}


def _json(payload: Any, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


@pytest.mark.asyncio
async def test_the_package_is_linked_and_kept_as_a_file_of_the_run(api):
    persisted = Persisted()
    package = _package()
    api.on(MDE, "/api/machineactions/A1", _json(_action()))
    # Defender quotes the link inside the JSON string.
    api.on(MDE, "/api/machineactions/A1/GetPackageUri", _json({"value": f'"{SAS}"'}))
    api.on(SAS_HOST, "/safedownload/WDATP_Investigation_Package.zip", httpx.Response(200, content=package))

    result = await _call(_executor(persist_file_fn=persisted), action_id="A1")

    assert not result.is_error
    assert persisted.calls == [("investigation-package-desktop-01.contoso.com.zip", package, "application/zip")]
    assert f"Download link (time-limited, valid for a short while only): {SAS}" in result.content
    assert "[[FILE:" in result.content
    assert "Contents (2 files):" in result.content and "- Processes/processes.csv (" in result.content
    assert not result.files
    download = next(r for r in api.requests if r.url.host == SAS_HOST)
    # The SAS link is pre-authenticated: the bearer token never goes to it.
    assert "authorization" not in {k.lower() for k in download.headers}


@pytest.mark.asyncio
async def test_a_package_still_being_collected_is_not_asked_for(api):
    api.on(MDE, "/api/machineactions/A1", _json(_action(status="InProgress")))

    result = await _call(_executor(), action_id="A1")

    assert "not ready yet (status: InProgress)" in result.content
    assert [r.url.path for r in api.requests] == ["/api/machineactions/A1"]


@pytest.mark.asyncio
async def test_another_kind_of_action_is_refused(api):
    api.on(MDE, "/api/machineactions/A1", _json(_action(kind="Isolate")))

    result = await _call(_executor(), action_id="A1")

    assert result.is_error and "is of type Isolate" in result.content


@pytest.mark.asyncio
async def test_a_package_over_the_cap_keeps_only_its_link(api, monkeypatch):
    monkeypatch.setattr(defender, "_MAX_PACKAGE_BYTES", 1024 * 1024)
    persisted = Persisted()
    api.on(MDE, "/api/machineactions/A1", _json(_action()))
    api.on(MDE, "/api/machineactions/A1/GetPackageUri", _json({"value": SAS}))
    api.on(SAS_HOST, "/safedownload/WDATP_Investigation_Package.zip", httpx.Response(200, content=b"P" * (1024 * 1024 + 1)))

    result = await _call(_executor(persist_file_fn=persisted), action_id="A1")

    assert "over the 1 MB download limit" in result.content and SAS in result.content
    assert persisted.calls == []


@pytest.mark.asyncio
async def test_an_expired_link_says_how_to_get_a_fresh_one(api):
    api.on(MDE, "/api/machineactions/A1", _json(_action()))
    api.on(MDE, "/api/machineactions/A1/GetPackageUri", _json({"value": SAS}))
    api.on(SAS_HOST, "/safedownload/WDATP_Investigation_Package.zip", httpx.Response(403, text="AuthenticationFailed"))

    result = await _call(_executor(persist_file_fn=Persisted()), action_id="A1")

    assert result.is_error and SAS in result.content
    assert "call get_defender_investigation_package again for a fresh one" in result.content


def test_a_long_entry_name_does_not_flood_the_listing():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x" * 60_000 + ".txt", "data")

    listing = defender._package_contents(buffer.getvalue())

    assert len(listing) < 400 and "Contents (1 files):" in listing


@pytest.mark.asyncio
async def test_a_link_to_a_non_routable_address_is_never_fetched(api):
    link = "https://169.254.169.254/latest/meta-data/package.zip"
    api.on(MDE, "/api/machineactions/A1", _json(_action()))
    api.on(MDE, "/api/machineactions/A1/GetPackageUri", _json({"value": link}))

    result = await _call(_executor(persist_file_fn=Persisted()), action_id="A1")

    assert result.is_error and "link was refused" in result.content
    assert "private or non-routable address" in result.content
    assert {r.url.host for r in api.requests} == {MDE}


@pytest.mark.asyncio
async def test_a_redirect_of_the_link_to_a_non_routable_address_is_never_followed(api):
    api.on(MDE, "/api/machineactions/A1", _json(_action()))
    api.on(MDE, "/api/machineactions/A1/GetPackageUri", _json({"value": SAS}))
    api.on(
        SAS_HOST,
        "/safedownload/WDATP_Investigation_Package.zip",
        httpx.Response(302, headers={"Location": "https://10.0.0.7/package.zip"}),
    )

    result = await _call(_executor(persist_file_fn=Persisted()), action_id="A1")

    assert result.is_error and "private or non-routable address" in result.content
    assert {r.url.host for r in api.requests} == {MDE, SAS_HOST}
    assert all("authorization" not in {k.lower() for k in r.headers} for r in api.requests if r.url.host == SAS_HOST)


def test_the_dispatcher_hands_defender_the_file_persistence_callback():
    assert "defender" in integration_tools._PERSIST_FILE_PROVIDERS
