"""The brand asset source: brand packs read from a GitHub folder at every asset seed.

Pins the folder URL forms, the bounded extraction of the repository archive
(only ``<path>/<pack>/<file>``, nothing deeper, nothing outside), the download
(the token reaches api.github.com only, failures worded for an administrator),
the seed running without the source when it cannot be read, and the admin
routes that set, clear and sync it.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

import app.services.brand_asset_source as mod
from app.services.brand_asset_source import (
    BrandAssetSourceError,
    BrandAssetSourceSync,
    GitHubFolder,
    extract_packs,
    parse_github_folder_url,
    zipball_url,
)
from app.services.skill_source import SkillCredential

INTEGRATION_ID = uuid4()
TOKEN = "ghp_BRANDSOURCETOKEN123456"


def _credential() -> SkillCredential:
    return SkillCredential(token=TOKEN, integration_id=INTEGRATION_ID, integration_name="Brand GitHub")


# ── The folder URL ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/brand", GitHubFolder("acme", "brand")),
        ("https://github.com/acme/brand.git", GitHubFolder("acme", "brand")),
        ("https://github.com/acme/brand/tree/main", GitHubFolder("acme", "brand", "main", "")),
        (
            "https://www.github.com/acme/brand/tree/v2/copilot/brand-assets/",
            GitHubFolder("acme", "brand", "v2", "copilot/brand-assets"),
        ),
        (
            "https://github.com/acme/brand/tree/v1%2E0/Brand%20Assets",
            GitHubFolder("acme", "brand", "v1.0", "Brand Assets"),
        ),
    ],
    ids=["repo", "dot-git", "ref", "ref-and-path", "percent-encoded"],
)
def test_a_github_folder_url_names_the_repository_ref_and_path(url, expected):
    assert parse_github_folder_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/brand",
        "https://gitlab.com/acme/brand",
        "https://github.com/acme",
        "https://github.com/acme/brand/blob/main/manifest.json",
        "https://github.com/acme/brand/tree",
        "https://github.com/acme/brand?tab=readme",
        "https://github.com:8443/acme/brand",
        "https://user@github.com/acme/brand",
        "https://github.com/acme/brand/tree/main/../secrets",
        "https://github.com/acme/brand/tree/main/%2E%2E/secrets",
        "https://github.com/acme/brand/tree/main/a%2Fb",
        "https://github.com/acme/" + "x" * 2100,
    ],
    ids=[
        "http",
        "other-host",
        "no-repo",
        "file-link",
        "tree-without-ref",
        "query",
        "port",
        "userinfo",
        "dot-dot",
        "encoded-dot-dot",
        "encoded-slash",
        "too-long",
    ],
)
def test_anything_but_a_github_folder_url_is_refused(url):
    with pytest.raises(BrandAssetSourceError):
        parse_github_folder_url(url)


def test_the_archive_url_encodes_what_the_url_carried():
    assert zipball_url(GitHubFolder("acme", "brand")) == "https://api.github.com/repos/acme/brand/zipball"
    assert (
        zipball_url(GitHubFolder("acme", "brand", "release/1"))
        == "https://api.github.com/repos/acme/brand/zipball/release%2F1"
    )


# ── The extraction ────────────────────────────────────────────────────────────


def _archive(files: dict[str, bytes], root: str = "acme-brand-0123abc") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{root}/", b"")
        for name, data in files.items():
            archive.writestr(f"{root}/{name}", data)
    return buffer.getvalue()


_MANIFEST = json.dumps({"assets": [{"file": "a.svg", "role": "partner_logo"}]}).encode()


def _extract(tmp_path: Path, data: bytes, path: str = "") -> tuple[tuple[str, ...], Path]:
    archive = tmp_path / "archive.zip"
    archive.write_bytes(data)
    dest = tmp_path / "packs"
    dest.mkdir(exist_ok=True)
    return extract_packs(archive, path, dest), dest


def test_only_the_pack_files_under_the_path_are_extracted(tmp_path):
    data = _archive(
        {
            "copilot/brand-assets/logos/manifest.json": _MANIFEST,
            "copilot/brand-assets/logos/a.svg": b"<svg/>",
            "copilot/brand-assets/logos/.DS_Store": b"x",
            "copilot/brand-assets/logos/nested/deep.svg": b"<svg/>",
            "copilot/brand-assets/README.md": b"top level",
            "copilot/brand-assets/Bad_Name/manifest.json": _MANIFEST,
            "copilot/brand-assets/no-manifest/a.svg": b"<svg/>",
            "elsewhere/logos/manifest.json": _MANIFEST,
            ".github/workflows/release.yml": b"on: push",
        }
    )
    packs, dest = _extract(tmp_path, data, "copilot/brand-assets")
    assert packs == ("logos",)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "logos/a.svg",
        "logos/manifest.json",
        "no-manifest/a.svg",
    ]


def test_the_repository_root_is_a_path_too(tmp_path):
    data = _archive({"logos/manifest.json": _MANIFEST, "logos/a.svg": b"<svg/>"})
    assert _extract(tmp_path, data)[0] == ("logos",)


def test_a_manifest_above_the_seed_ceiling_refuses_the_folder(tmp_path, monkeypatch):
    """Skipped, the pack would vanish and the rows it fed read as removed."""
    monkeypatch.setattr(mod, "MAX_SEED_ASSET_BYTES", 10)
    with pytest.raises(BrandAssetSourceError, match="manifest of pack logos"):
        _extract(tmp_path, _archive({"logos/manifest.json": b"x" * 11}))


def test_a_file_declared_above_the_seed_ceiling_is_not_extracted(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MAX_SEED_ASSET_BYTES", 10)
    data = _archive({"logos/manifest.json": b"{}", "logos/big.png": b"x" * 11})
    packs, dest = _extract(tmp_path, data)
    assert packs == ("logos",)
    assert not (dest / "logos" / "big.png").exists()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"logos/a.svg": b"<svg/>"}, "No brand pack"),
        ({f"logos/{i}.svg": b"x" for i in range(4)} | {"logos/manifest.json": b"{}"}, "more than 3"),
    ],
    ids=["no-pack", "too-many-files"],
)
def test_a_folder_with_no_pack_or_too_many_files_is_refused(tmp_path, monkeypatch, files, message):
    monkeypatch.setattr(mod, "MAX_FILES", 3)
    with pytest.raises(BrandAssetSourceError, match=message):
        _extract(tmp_path, _archive(files))


def test_packs_past_the_extraction_budget_or_a_broken_archive_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MAX_EXTRACTED_BYTES", 8)
    with pytest.raises(BrandAssetSourceError, match="cannot be read"):
        _extract(tmp_path, _archive({"logos/manifest.json": b"0123456789"}))
    with pytest.raises(BrandAssetSourceError, match="cannot be read"):
        _extract(tmp_path, b"not a zip")


# ── The download ──────────────────────────────────────────────────────────────


def _transport(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _client(*args, **kwargs):
        kwargs.pop("route", None)
        return httpx.AsyncClient(*args, transport=httpx.MockTransport(_record), **kwargs)

    monkeypatch.setattr(mod, "egress_client", _client)
    return seen


def test_the_token_reaches_the_api_only_and_the_archive_comes_back(monkeypatch, tmp_path):
    body = _archive({"logos/manifest.json": _MANIFEST})

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/brand/zip/main"})
        return httpx.Response(200, content=body)

    seen = _transport(monkeypatch, handler)
    target = tmp_path / "archive.zip"
    asyncio.run(mod.download_archive(GitHubFolder("acme", "brand", "main"), _credential(), target))
    assert target.read_bytes() == body, "streamed to disk as it came"
    assert [r.url.host for r in seen] == ["api.github.com", "codeload.github.com"]
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in seen[1].headers, "the redirect to codeload drops the token"


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (401, {}, "rejected the token of integration 'Brand GitHub'"),
        (403, {"x-ratelimit-remaining": "0"}, "throttling"),
        (404, {}, "Contents: Read"),
        (500, {}, "HTTP 500"),
    ],
    ids=["auth", "rate-limit", "not-found", "server"],
)
def test_a_failed_download_says_what_to_fix_and_never_the_token(monkeypatch, tmp_path, status, headers, message):
    _transport(monkeypatch, lambda request: httpx.Response(status, headers=headers))
    with pytest.raises(BrandAssetSourceError, match=message) as err:
        asyncio.run(mod.download_archive(GitHubFolder("acme", "brand"), _credential(), tmp_path / "a.zip"))
    assert TOKEN not in str(err.value)


def test_an_archive_past_the_download_limit_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "MAX_ARCHIVE_BYTES", 10)
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(BrandAssetSourceError, match="smaller repository"):
        asyncio.run(mod.download_archive(GitHubFolder("acme", "brand"), _credential(), tmp_path / "a.zip"))


def test_github_out_of_reach_is_a_source_error(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    _transport(monkeypatch, handler)
    with pytest.raises(BrandAssetSourceError, match="could not be reached"):
        asyncio.run(mod.download_archive(GitHubFolder("acme", "brand"), _credential(), tmp_path / "a.zip"))


def test_the_credential_errors_name_the_brand_asset_source():
    """The skill credential rules, worded for this source."""

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    with pytest.raises(BrandAssetSourceError, match="No GitHub integration is selected"):
        asyncio.run(mod.resolve_source_credential(_Db(), None))
    with pytest.raises(BrandAssetSourceError) as err:
        asyncio.run(mod.resolve_source_credential(_Db(), INTEGRATION_ID))
    assert "bound to this brand asset source no longer exists" in str(err.value)
    assert "in Settings > Platform" in str(err.value)


# ── The seed with the source ──────────────────────────────────────────────────


FOLDER = GitHubFolder("acme", "brand", "main", "packs")
SETTING = ("https://github.com/acme/brand/tree/main/packs", INTEGRATION_ID)


def _patch_seed(monkeypatch, *, source, files=None, bundled=()):
    """*source*: what ``_source_and_credential`` answers (a value, or an exception
    to raise); *files*: the archive GitHub serves."""
    import app.core.database as database
    import app.services.template_asset_seed as seed_mod

    @asynccontextmanager
    async def _session():
        yield object()

    monkeypatch.setattr(database, "async_session", _session)
    if isinstance(source, BaseException):
        monkeypatch.setattr(mod, "_source_and_credential", AsyncMock(side_effect=source))
    else:
        monkeypatch.setattr(mod, "_source_and_credential", AsyncMock(return_value=source))

    async def _download(folder, credential, target: Path):
        target.write_bytes(_archive(files or {}))

    download = AsyncMock(side_effect=_download)
    monkeypatch.setattr(mod, "download_archive", download)
    monkeypatch.setattr(mod, "_bundled_pack_names", lambda: set(bundled))
    calls: list[tuple] = []

    async def _seed(_db, *, platform_mode, extra_roots=(), retire=True):
        calls.append(
            (platform_mode, tuple(extra_roots), [sorted(p.name for p in r.iterdir()) for r in extra_roots], retire)
        )
        return 3

    monkeypatch.setattr(seed_mod, "seed_builtin_template_assets", _seed)
    return calls, download


def test_no_source_seeds_from_the_bundled_packs_alone(monkeypatch):
    calls, download = _patch_seed(monkeypatch, source=None)
    assert asyncio.run(mod.seed_template_assets("copilot")) == (3, None)
    assert calls == [("copilot", (), [], True)]
    download.assert_not_awaited()


def test_the_downloaded_packs_are_one_more_root_while_the_seed_runs(monkeypatch):
    calls, _ = _patch_seed(
        monkeypatch,
        source=(FOLDER, _credential(), SETTING),
        files={"packs/logos/manifest.json": _MANIFEST, "packs/logos/a.svg": b"<svg/>"},
    )
    uploaded, sync = asyncio.run(mod.seed_template_assets("copilot"))
    assert (uploaded, sync) == (3, BrandAssetSourceSync(packs=("logos",), setting=SETTING))
    ((mode, roots, listed, retire),) = calls
    assert mode == "copilot" and listed == [["logos"]] and retire
    assert not roots[0].exists(), "the temporary folder goes with the seed"


def test_a_pack_the_platform_also_ships_is_reported_and_not_read(monkeypatch):
    """A pack name loads once, the bundled root first: the source's copy is not
    used, and the sync says so instead of counting it as read."""
    calls, _ = _patch_seed(
        monkeypatch,
        source=(FOLDER, _credential(), SETTING),
        files={"packs/logos/manifest.json": _MANIFEST, "packs/badges/manifest.json": _MANIFEST},
        bundled={"badges"},
    )
    _uploaded, sync = asyncio.run(mod.seed_template_assets("copilot"))
    assert sync == BrandAssetSourceSync(packs=("logos",), shadowed=("badges",), setting=SETTING)
    ((_mode, _roots, _listed, retire),) = calls
    assert retire, "the source was read: retirements run as usual"


def test_a_source_that_cannot_be_read_leaves_the_seed_running_without_it(monkeypatch, caplog):
    calls, _ = _patch_seed(monkeypatch, source=BrandAssetSourceError("GitHub returned HTTP 500 for acme/brand."))
    uploaded, sync = asyncio.run(mod.seed_template_assets("copilot"))
    assert uploaded == 3
    assert sync == BrandAssetSourceSync(error="GitHub returned HTTP 500 for acme/brand.")
    # No extra root, and no template retires anything: a pack of the source
    # may feed a template directly, which no include reveals.
    assert calls == [("copilot", (), [], False)]
    assert "Brand asset source not read" in caplog.text
    assert "every template keeps its seeded assets" in caplog.text


def test_any_other_failure_of_the_source_never_stops_the_seed(monkeypatch, caplog):
    """A stored token that no longer decrypts (an encryption key change), a
    disk error: the boot seed still runs, without the source, retiring nothing."""
    from cryptography.fernet import InvalidToken

    calls, _ = _patch_seed(monkeypatch, source=InvalidToken())
    uploaded, sync = asyncio.run(mod.seed_template_assets("copilot"))
    assert uploaded == 3
    assert sync == BrandAssetSourceSync(error="The brand asset source could not be read (InvalidToken).")
    assert calls == [("copilot", (), [], False)]
    assert "InvalidToken" in caplog.text, "logged with its traceback"


def test_no_source_creates_no_temporary_folder(monkeypatch):
    """Every boot reads the setting; without a source nothing touches the disk."""
    _patch_seed(monkeypatch, source=None)

    def _no_tmp(*args, **kwargs):
        raise AssertionError("no temporary folder without a source")

    monkeypatch.setattr(mod.tempfile, "TemporaryDirectory", _no_tmp)
    assert asyncio.run(mod.seed_template_assets("copilot")) == (3, None)


def test_a_download_past_its_deadline_is_a_source_not_read(monkeypatch):
    """Per-read timeouts do not bound a stream that trickles; the deadline does."""
    calls, download = _patch_seed(monkeypatch, source=(FOLDER, _credential(), SETTING))
    monkeypatch.setattr(mod, "DOWNLOAD_DEADLINE_SECONDS", 0.01)

    async def _slow(folder, credential, target):
        await asyncio.sleep(1)

    download.side_effect = _slow
    _uploaded, sync = asyncio.run(mod.seed_template_assets("copilot"))
    assert sync is not None and "within" in (sync.error or "")
    assert calls == [("copilot", (), [], False)]


def test_the_download_runs_after_the_session_is_closed(monkeypatch, tmp_path):
    """The URL and the credential are read in a session closed before the
    download: no pooled connection sits idle in a transaction meanwhile."""
    import app.core.database as database

    state = {"open": False}

    @asynccontextmanager
    async def _session():
        state["open"] = True
        try:
            yield object()
        finally:
            state["open"] = False

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(
        mod, "configured_source", AsyncMock(return_value=("https://github.com/acme/brand", INTEGRATION_ID))
    )
    monkeypatch.setattr(mod, "resolve_source_credential", AsyncMock(return_value=_credential()))
    seen: list[bool] = []

    async def _download(folder, credential, target: Path):
        seen.append(state["open"])
        target.write_bytes(_archive({"logos/manifest.json": _MANIFEST}))

    monkeypatch.setattr(mod, "download_archive", _download)
    monkeypatch.setattr(mod, "_bundled_pack_names", set)

    async def _run():
        async with mod.downloaded_source() as (_root, sync):
            return sync

    assert asyncio.run(_run()) == BrandAssetSourceSync(
        packs=("logos",), setting=("https://github.com/acme/brand", INTEGRATION_ID)
    )
    assert seen == [False]


# ── The admin routes ──────────────────────────────────────────────────────────


def _admin():
    return SimpleNamespace(id=uuid4(), is_admin=True, role="admin", email="admin@example.com", full_name="Admin")


class _Db:
    def __init__(self, name="Brand GitHub"):
        self.commits = 0
        self._name = name

    async def execute(self, _query):
        return SimpleNamespace(scalar_one_or_none=lambda: self._name)

    async def commit(self):
        self.commits += 1


def _patch_routes(monkeypatch, ps, *, integration=None, credential_error=None):
    import app.api.v1.platform_settings_helpers as helpers
    import app.api.v1.settings as settings_mod

    monkeypatch.setattr(helpers, "get_platform_settings", AsyncMock(return_value=ps))
    audit = AsyncMock()
    monkeypatch.setattr(settings_mod, "log_audit", audit)
    monkeypatch.setattr(
        settings_mod,
        "get_user_integration",
        AsyncMock(return_value=integration or SimpleNamespace(id=INTEGRATION_ID, provider="github")),
    )
    monkeypatch.setattr(
        mod,
        "resolve_source_credential",
        AsyncMock(side_effect=credential_error, return_value=_credential()),
    )
    sync = AsyncMock(return_value=settings_mod.BrandAssetSourceSyncResult(packs=["logos"], assets_uploaded=2))
    monkeypatch.setattr(settings_mod, "_sync_brand_asset_source", sync)
    return settings_mod, audit, sync


def _ps(url=None, integration_id=None):
    return SimpleNamespace(brand_assets_source_url=url, brand_assets_source_integration_id=integration_id)


def test_saving_a_source_stores_it_audits_commits_then_syncs(monkeypatch):
    ps = _ps()
    settings_mod, audit, sync = _patch_routes(monkeypatch, ps)
    db = _Db()
    body = settings_mod.BrandAssetSourceConfig(
        url=" https://github.com/acme/brand/tree/main/packs ", integration_id=str(INTEGRATION_ID)
    )
    response = asyncio.run(
        settings_mod.update_brand_asset_source(body=body, request=SimpleNamespace(), user=_admin(), db=db)
    )
    assert ps.brand_assets_source_url == "https://github.com/acme/brand/tree/main/packs"
    assert ps.brand_assets_source_integration_id == INTEGRATION_ID
    assert db.commits == 1 and audit.await_count == 1
    sync.assert_awaited_once()
    assert response.url == ps.brand_assets_source_url
    assert response.integration_id == str(INTEGRATION_ID)
    assert response.integration_name == "Brand GitHub"
    assert response.sync.packs == ["logos"]


def test_clearing_the_source_syncs_nothing(monkeypatch):
    ps = _ps("https://github.com/acme/brand", INTEGRATION_ID)
    settings_mod, audit, sync = _patch_routes(monkeypatch, ps)
    response = asyncio.run(
        settings_mod.update_brand_asset_source(
            body=settings_mod.BrandAssetSourceConfig(url=""), request=SimpleNamespace(), user=_admin(), db=_Db(None)
        )
    )
    assert ps.brand_assets_source_url is None and ps.brand_assets_source_integration_id is None
    assert audit.await_count == 1
    sync.assert_not_awaited()
    assert response.sync is None


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"url": "https://gitlab.com/acme/brand", "integration_id": str(INTEGRATION_ID)}, 400, "GitHub folder URL"),
        ({"url": "https://github.com/acme/brand"}, 400, "Select the GitHub integration"),
        ({"url": "https://github.com/acme/brand", "integration_id": str(INTEGRATION_ID)}, 422, "acts with each"),
    ],
    ids=["bad-url", "no-integration", "credential-refused"],
)
def test_a_source_that_cannot_work_is_refused_before_it_is_saved(monkeypatch, body, status, detail):
    ps = _ps()
    settings_mod, _audit, sync = _patch_routes(
        monkeypatch, ps, credential_error=BrandAssetSourceError("Integration 'X' acts with each person's own account")
    )
    db = _Db()
    with pytest.raises(HTTPException) as err:
        asyncio.run(
            settings_mod.update_brand_asset_source(
                body=settings_mod.BrandAssetSourceConfig(**body), request=SimpleNamespace(), user=_admin(), db=db
            )
        )
    assert err.value.status_code == status and detail in err.value.detail
    assert ps.brand_assets_source_url is None and db.commits == 0
    sync.assert_not_awaited()


def test_a_sync_without_a_source_says_so(monkeypatch):
    import app.api.v1.settings as settings_mod
    import app.core.migrations as migrations
    import app.services.ai_state as ai_state

    @asynccontextmanager
    async def _lock(wait_timeout=None):
        yield

    monkeypatch.setattr(migrations, "bootstrap_lock", _lock)
    monkeypatch.setattr(ai_state, "content_mode", lambda: "copilot")

    @asynccontextmanager
    async def _downloaded():
        yield Path("/nowhere"), None

    monkeypatch.setattr(mod, "downloaded_source", _downloaded)
    result = asyncio.run(settings_mod._sync_brand_asset_source())
    assert result.error == "No brand asset source is set."


def test_a_sync_downloads_before_it_takes_the_seed_lock(monkeypatch):
    """A slow GitHub must not hold the lock a booting replica waits on."""
    import app.api.v1.settings as settings_mod
    import app.core.migrations as migrations
    import app.services.ai_state as ai_state

    order: list[str] = []

    @asynccontextmanager
    async def _lock(wait_timeout=None):
        order.append("lock")
        yield
        order.append("unlock")

    @asynccontextmanager
    async def _downloaded():
        order.append("download")
        yield Path("/nowhere"), BrandAssetSourceSync(packs=("logos",), shadowed=("badges",))

    async def _seed(mode, root, sync):
        order.append("seed")
        return 2

    monkeypatch.setattr(migrations, "bootstrap_lock", _lock)
    monkeypatch.setattr(ai_state, "content_mode", lambda: "copilot")
    monkeypatch.setattr(mod, "downloaded_source", _downloaded)
    monkeypatch.setattr(mod, "seed_with_source", _seed)
    monkeypatch.setattr(mod, "source_unchanged", AsyncMock(return_value=True))
    result = asyncio.run(settings_mod._sync_brand_asset_source())
    assert order == ["download", "lock", "seed", "unlock"]
    assert (result.packs, result.shadowed, result.assets_uploaded) == (["logos"], ["badges"], 2)


def test_a_sync_whose_source_changed_meanwhile_seeds_nothing(monkeypatch):
    """Sync now downloading URL1 while a save sets URL2: seeding URL1 after
    URL2 would retire what URL2 just seeded."""
    import app.api.v1.settings as settings_mod
    import app.core.migrations as migrations
    import app.services.ai_state as ai_state

    @asynccontextmanager
    async def _lock(wait_timeout=None):
        yield

    @asynccontextmanager
    async def _downloaded():
        yield Path("/nowhere"), BrandAssetSourceSync(packs=("logos",), setting=SETTING)

    seed = AsyncMock(return_value=2)
    monkeypatch.setattr(migrations, "bootstrap_lock", _lock)
    monkeypatch.setattr(ai_state, "content_mode", lambda: "copilot")
    monkeypatch.setattr(mod, "downloaded_source", _downloaded)
    monkeypatch.setattr(mod, "seed_with_source", seed)
    monkeypatch.setattr(mod, "_read_configured_source", AsyncMock(return_value=("https://github.com/acme/other", None)))
    result = asyncio.run(settings_mod._sync_brand_asset_source())
    seed.assert_not_awaited()
    assert result.error == "The brand asset source changed while it was read; sync again."


def test_sync_now_ends_the_auth_transaction_before_the_download(monkeypatch):
    import app.api.v1.settings as settings_mod

    events: list[str] = []

    class _TrackedDb(_Db):
        async def commit(self):
            events.append("commit")

    async def _sync():
        events.append("sync")
        return settings_mod.BrandAssetSourceSyncResult(packs=[])

    monkeypatch.setattr(settings_mod, "_sync_brand_asset_source", _sync)
    monkeypatch.setattr(settings_mod, "_brand_asset_source_response", AsyncMock(return_value=None))
    asyncio.run(settings_mod.sync_brand_asset_source(user=_admin(), db=_TrackedDb()))
    assert events == ["commit", "sync"]


def test_the_brand_asset_source_routes_are_admin_only():
    from app.api.deps import require_admin
    from app.api.v1.settings import router

    routes = [r for r in router.routes if getattr(r, "path", "").startswith("/brand-asset-source")]
    assert {(r.path, tuple(sorted(r.methods))) for r in routes} == {
        ("/brand-asset-source", ("GET",)),
        ("/brand-asset-source", ("PUT",)),
        ("/brand-asset-source/sync", ("POST",)),
    }
    for route in routes:
        assert require_admin in [d.call for d in route.dependant.dependencies], route.path
