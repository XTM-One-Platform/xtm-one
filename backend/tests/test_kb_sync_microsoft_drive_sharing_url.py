"""Tests for SharePoint sharing-URL resolution in the microsoft-drive KB sync.

Covers:
- _is_sharepoint_sharing_url() detection helper
- _resolve_drive_sharing_url() Graph API call + error handling
- sync_microsoft_drive() correctly routes sharing URLs through the resolver
  and builds the right start_url / base without hitting the invalid
  /root:/<url>:/children Graph endpoint.
"""

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.kb_sync import (
    GRAPH_API,
    _is_sharepoint_sharing_url,
    _resolve_drive_sharing_url,
)

# ---------------------------------------------------------------------------
# _is_sharepoint_sharing_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Typical folder sharing link
        (
            "https://contoso.sharepoint.com/:f:/s/mysite/AABB1234?e=abc",
            True,
        ),
        # File sharing link
        (
            "https://contoso.sharepoint.com/:u:/s/mysite/AABB1234?e=abc",
            True,
        ),
        # Blob sharing link
        (
            "https://contoso.sharepoint.com/:b:/s/mysite/AABB1234",
            True,
        ),
        # HTTP (not HTTPS)
        (
            "http://contoso.sharepoint.com/:f:/s/mysite/AABB",
            True,
        ),
        # Regular folder path — NOT a sharing URL
        ("/Documents/MyFolder", False),
        ("Documents/Sub", False),
        ("/", False),
        ("", False),
        # OneDrive personal URL without colon-scheme
        ("https://onedrive.live.com/view.aspx?resid=ABC", False),
        # SharePoint site URL without the sharing path pattern
        ("https://contoso.sharepoint.com/sites/mysite", False),
        # Composite site ID
        ("contoso.sharepoint.com,abc-123,def-456", False),
    ],
)
def test_is_sharepoint_sharing_url(url, expected):
    assert _is_sharepoint_sharing_url(url) is expected


# ---------------------------------------------------------------------------
# _resolve_drive_sharing_url
# ---------------------------------------------------------------------------


def _make_httpx_response(status_code: int, json_body: dict):
    """Build a minimal fake httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


@pytest.mark.asyncio
async def test_resolve_drive_sharing_url_success():
    sharing_url = "https://contoso.sharepoint.com/:f:/s/mysite/AABB1234?e=token"
    drive_id = "b!abc123"
    item_id = "01XYZABC"

    fake_item = {
        "id": item_id,
        "parentReference": {"driveId": drive_id},
    }

    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_httpx_response(200, fake_item))
    headers = {"Authorization": "Bearer tok"}

    base, start_url = await _resolve_drive_sharing_url(client, headers, sharing_url)

    # Verify the share_id encoding
    encoded = base64.urlsafe_b64encode(sharing_url.encode()).rstrip(b"=").decode()
    expected_share_id = f"u!{encoded}"
    client.get.assert_awaited_once_with(
        f"{GRAPH_API}/shares/{expected_share_id}/driveItem",
        headers=headers,
    )

    assert base == f"{GRAPH_API}/drives/{drive_id}"
    assert start_url == f"{GRAPH_API}/drives/{drive_id}/items/{item_id}/children"


@pytest.mark.asyncio
async def test_resolve_drive_sharing_url_raises_on_400():
    sharing_url = "https://contoso.sharepoint.com/:f:/s/mysite/AABB?e=bad"

    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_httpx_response(400, {"error": "bad"}))
    headers = {"Authorization": "Bearer tok"}

    with pytest.raises(ValueError, match="Failed to resolve SharePoint sharing URL"):
        await _resolve_drive_sharing_url(client, headers, sharing_url)


@pytest.mark.asyncio
async def test_resolve_drive_sharing_url_raises_on_missing_drive_id():
    sharing_url = "https://contoso.sharepoint.com/:f:/s/mysite/AABB?e=token"

    # Response missing driveId
    fake_item = {"id": "01XYZABC", "parentReference": {}}

    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_httpx_response(200, fake_item))
    headers = {"Authorization": "Bearer tok"}

    with pytest.raises(ValueError, match="Could not extract driveId/itemId"):
        await _resolve_drive_sharing_url(client, headers, sharing_url)


@pytest.mark.asyncio
async def test_resolve_drive_sharing_url_raises_on_missing_item_id():
    sharing_url = "https://contoso.sharepoint.com/:f:/s/mysite/AABB?e=token"

    # Response missing item id
    fake_item = {"parentReference": {"driveId": "b!abc123"}}

    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_httpx_response(200, fake_item))
    headers = {"Authorization": "Bearer tok"}

    with pytest.raises(ValueError, match="Could not extract driveId/itemId"):
        await _resolve_drive_sharing_url(client, headers, sharing_url)


# ---------------------------------------------------------------------------
# sync_microsoft_drive — routing for sharing URLs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_microsoft_drive_sharing_url_uses_resolver(monkeypatch):
    """When folder_path is a sharing URL, sync_microsoft_drive must call
    _resolve_drive_sharing_url and use the returned (base, start_url) — it
    must NOT construct the invalid /root:/<url>:/children URL."""
    from app.services import kb_sync

    sharing_url = "https://contoso.sharepoint.com/:f:/s/mysite/AABB?e=token"
    drive_id = "b!drive123"
    item_id = "01ITEM456"

    resolved_base = f"{GRAPH_API}/drives/{drive_id}"
    resolved_start = f"{resolved_base}/items/{item_id}/children"

    # Patch _get_integration_token so no DB is needed
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="fake-token"),
    )

    # Patch _resolve_drive_sharing_url to return deterministic values
    monkeypatch.setattr(
        kb_sync,
        "_resolve_drive_sharing_url",
        AsyncMock(return_value=(resolved_base, resolved_start)),
    )

    # Stub out post-sync DB helpers so we don't need a real session
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    # Track every URL that httpx would call
    called_urls: list[str] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            # Empty children page — no files to process
            return {"value": []}

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **_kwargs):
            called_urls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: _FakeAsyncClient())

    db = MagicMock()
    db.commit = AsyncMock()

    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    doc_ids = await kb_sync.sync_microsoft_drive(
        db,
        kb,
        user_id=uuid.uuid4(),
        folder_path=sharing_url,
    )

    # The resolver must have been called
    kb_sync._resolve_drive_sharing_url.assert_awaited_once()

    # The only children URL hit must be the resolved one — never the sharing URL embedded in a path
    assert called_urls == [resolved_start], (
        f"Expected only [{resolved_start!r}], got {called_urls!r}"
    )

    # No documents — empty list returned
    assert doc_ids == []


@pytest.mark.asyncio
async def test_sync_microsoft_drive_plain_path_does_not_use_resolver(monkeypatch):
    """A plain folder path must NOT invoke _resolve_drive_sharing_url."""
    from app.services import kb_sync

    resolver_mock = AsyncMock(return_value=("base", "url"))
    monkeypatch.setattr(kb_sync, "_resolve_drive_sharing_url", resolver_mock)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="fake-token"),
    )

    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    called_urls: list[str] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"value": []}

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **_kwargs):
            called_urls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: _FakeAsyncClient())

    db = MagicMock()
    db.commit = AsyncMock()

    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    await kb_sync.sync_microsoft_drive(
        db,
        kb,
        user_id=uuid.uuid4(),
        folder_path="/Documents/MyFolder",
    )

    resolver_mock.assert_not_awaited()

    # The URL used must be the standard /root:/<path>:/children form
    assert len(called_urls) == 1
    assert "root:/Documents/MyFolder:/children" in called_urls[0]


# ---------------------------------------------------------------------------
# _resolve_sharepoint_site_id — sharing URL as site_id raises KBSyncConfigError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sharing_site_id",
    [
        # Folder sharing link used as site_id
        "https://filigranio.sharepoint.com/:f:/s/MySite/AABB1234?e=token",
        # File sharing link used as site_id
        "https://contoso.sharepoint.com/:u:/s/mysite/AABB1234?e=abc",
        # Blob sharing link
        "https://contoso.sharepoint.com/:b:/s/mysite/AABB1234",
    ],
)
async def test_resolve_sharepoint_site_id_rejects_sharing_url(sharing_site_id):
    """A sharing URL (/:<type>:/s/... — e.g. /:f:, /:u:, /:b:) passed as
    site_id must raise KBSyncConfigError immediately — without making any
    Graph API calls. This fast-fails the sync and lets the scheduler disable
    the source instead of retrying a doomed config daily.
    """
    from app.services.kb_sync import KBSyncConfigError, _resolve_sharepoint_site_id

    client = AsyncMock()

    with pytest.raises(KBSyncConfigError, match="sharing link") as exc_info:
        await _resolve_sharepoint_site_id(client, {}, sharing_site_id)

    # Must not touch the Graph API — the error is config-level, not network
    client.get.assert_not_awaited()

    # The message is persisted to last_sync_error, logged, and broadcast via
    # SSE — it must never echo the share token (the encoded path segment and
    # the ?e= query parameter grant access to the shared resource).
    message = str(exc_info.value)
    assert "AABB1234" not in message, "encoded share id must be redacted"
    assert "?e=" not in message, "share token query parameter must be redacted"


@pytest.mark.asyncio
async def test_resolve_sharepoint_site_id_accepts_canonical_url():
    """A canonical SharePoint site URL must pass the sharing-link guard and
    proceed to Graph API resolution normally (no KBSyncConfigError)."""
    from app.services.kb_sync import GRAPH_API, _resolve_sharepoint_site_id

    canonical_url = "https://contoso.sharepoint.com/sites/my-site"

    # Simulate Graph API /sites/hostname:/path returning the resolved id
    resolved_id = "contoso.sharepoint.com,abc-123,def-456"
    path_resp = MagicMock()
    path_resp.status_code = 200
    path_resp.json.return_value = {"id": resolved_id}

    client = AsyncMock()
    client.get = AsyncMock(return_value=path_resp)

    result = await _resolve_sharepoint_site_id(client, {}, canonical_url)

    assert result == resolved_id
    # The path-based resolution endpoint must have been called
    expected_url = f"{GRAPH_API}/sites/contoso.sharepoint.com:/sites/my-site"
    client.get.assert_awaited_once_with(expected_url, headers={})

