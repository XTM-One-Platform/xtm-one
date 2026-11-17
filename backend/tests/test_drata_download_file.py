"""Tests for the Drata ``download_drata_file`` tool.

The Drata get_* document tools only return a short-lived signed download URL,
not bytes. ``download_drata_file`` fetches the bytes server-side and registers
them as a forwardable file_id (a download card) - the same hop as Notion's
``download_notion_file`` / Figma's ``download_figma_image`` - so a Drata policy
file / evidence artifact / vendor-user-risk document can be re-used elsewhere
(e.g. re-filed as evidence via source_file_id). HTTP is mocked at
``drata._common.safe_request``.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall
from tests.integration_file_fakes import fenced_and_open

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_SIGNED = (
    "https://drata-prod-uploads.s3.us-east-1.amazonaws.com/evidence/policy.pdf"
    "?X-Amz-Signature=abc123"
)


class _BytesResp:
    def __init__(self, content: bytes, content_type: str = "application/pdf") -> None:
        self.content = content
        self.status_code = 200
        self.headers = {"content-type": content_type}

    def json(self) -> dict:
        return {}


def _patch_bytes(monkeypatch, content: bytes, content_type: str = "application/pdf"):
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url})
        return _BytesResp(content, content_type), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


def test_download_drata_file_in_tool_surface():
    executor = CallableToolExecutor()
    defs = drata.build_tools(_TOKEN, executor)
    tool = next(t for t in defs if t.name == "download_drata_file")
    assert tool.input_schema["required"] == ["url"]
    assert "url" in tool.input_schema["properties"]
    assert tool.source == "integration:drata"


@pytest.mark.asyncio
async def test_download_drata_file_fetches_bytes_into_file_id(monkeypatch):
    calls = _patch_bytes(monkeypatch, b"%PDF-1.4 drata policy")
    executor = _build()
    result = await _call(executor, "download_drata_file", {"url": _SIGNED})
    assert not result.is_error, result.content
    # The name and the signed URL come from Drata: inside the untrusted
    # block only; the marker and the file_id stay outside it.
    inside, outside = fenced_and_open(result.content)
    assert "Name: policy.pdf" in inside.splitlines()
    assert f"Download URL (signed by Drata, short-lived): {_SIGNED}" in inside.splitlines()
    assert "policy.pdf" not in outside
    assert outside.count(f"[[FILE:{result.files[0].file_id}]]") == 1
    assert result.files and len(result.files) == 1
    assert result.files[0].filename == "policy.pdf"
    assert result.files[0].content_type == "application/pdf"
    assert calls and calls[0]["url"] == _SIGNED


@pytest.mark.asyncio
async def test_download_drata_file_filename_override(monkeypatch):
    _patch_bytes(monkeypatch, b"bytes", "image/png")
    executor = _build()
    result = await _call(
        executor, "download_drata_file", {"url": _SIGNED, "filename": "renamed.png"}
    )
    assert not result.is_error, result.content
    assert result.files[0].filename == "renamed.png"


@pytest.mark.asyncio
async def test_download_drata_file_rejects_foreign_host(monkeypatch):
    calls = _patch_bytes(monkeypatch, b"x")
    executor = _build()
    result = await _call(
        executor,
        "download_drata_file",
        {"url": "https://169.254.169.254/latest/meta-data/"},
    )
    assert result.is_error
    assert "refusing to download" in result.content
    assert calls == [], "must not fetch from a non-allow-listed host"


@pytest.mark.asyncio
async def test_download_drata_file_requires_url(monkeypatch):
    _patch_bytes(monkeypatch, b"x")
    executor = _build()
    result = await _call(executor, "download_drata_file", {})
    assert result.is_error
    assert "url is required" in result.content


@pytest.mark.asyncio
async def test_download_drata_file_rejects_non_https(monkeypatch):
    calls = _patch_bytes(monkeypatch, b"x")
    executor = _build()
    result = await _call(
        executor,
        "download_drata_file",
        {"url": "http://drata-prod-uploads.s3.amazonaws.com/x.pdf"},
    )
    assert result.is_error
    assert "HTTPS" in result.content
    assert calls == []


@pytest.mark.asyncio
async def test_download_drata_file_empty_body(monkeypatch):
    _patch_bytes(monkeypatch, b"")
    executor = _build()
    result = await _call(executor, "download_drata_file", {"url": _SIGNED})
    assert result.is_error
    assert "empty" in result.content
