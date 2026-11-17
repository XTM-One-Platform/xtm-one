# SPDX-License-Identifier: Apache-2.0

"""Brand asset packs read from a GitHub folder (Settings > Platform).

The asset seed reads the packs the image ships (``brand_assets/``) and the
folders ``BRAND_ASSETS_EXTRA_DIRS`` names (``template_asset_seed``). This adds a
third source, for a deployment that can change neither its image nor its
environment: an administrator names a GitHub folder,
``https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]``, and the GitHub
integration whose token reads it (``platform_settings.brand_assets_source_url``
and ``brand_assets_source_integration_id``). Every asset seed (each boot, and
each save or sync from the settings page) downloads the repository's archive at
that ref (the default branch when the URL names none), extracts the packs under
the path into a temporary folder and reads it as one more root.

Only the files the loader reads are extracted: ``<pack>/<file>``, one level
under the path, a pack folder named as an ``include`` names one. The download
and the extraction are bounded (:data:`MAX_ARCHIVE_BYTES`,
:data:`MAX_EXTRACTED_BYTES`, :data:`MAX_FILES`), a member's bytes read through
``archive_reading``. Nothing is cached: the seeded rows and their objects are
what persists. A read that fails (GitHub unreachable, a token revoked or
under-scoped, a path gone, a stored secret that no longer decrypts) is reported
and the seed runs without the folder, retiring nothing in any template. The
download runs outside ``bootstrap_lock`` and outside any database session
(:func:`downloaded_source`); only the seed takes the lock (:func:`seed_with_source`).

The credential is a skill source's (``skill_source.resolve_skill_credential``):
a connected GitHub integration that shares its account, its token in the
database, never in the environment; the request takes the integration's relay
route.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError
from app.core.egress import egress_client, egress_route
from app.integrations.providers.github import GITHUB_API
from app.models.platform_settings import PlatformSettings
from app.services.skill_source import (
    SkillCredential,
    SkillCredentialError,
    classify_github_failure,
    resolve_skill_credential,
)
from app.services.template_asset_seed import MAX_SEED_ASSET_BYTES, PACK_NAME_RE

logger = logging.getLogger(__name__)

#: The most the repository archive may weigh, downloaded: the source is meant
#: to be a folder of logos, and the archive holds the whole repository.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024

#: The most the extracted packs may weigh in all, and how many files they may hold.
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000

#: The longest URL the setting stores (``platform_settings.brand_assets_source_url``).
MAX_URL_LENGTH = 2048

#: The download's timeouts: GitHub out of reach fails within the connect
#: timeout, so a boot without GitHub does not wait the read timeout out.
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

#: The most a whole download may take: per-read timeouts do not bound a
#: stream that trickles, and the boot downloads under ``bootstrap_lock``.
DOWNLOAD_DEADLINE_SECONDS = 180

_SUBJECT = "brand asset source"
_REBIND_HINT = "in Settings > Platform"
_GITHUB_WEB_HOSTS = frozenset({"github.com", "www.github.com"})
_API_VERSION = "2022-11-28"


class BrandAssetSourceError(Exception):
    """The source cannot be read: a bad URL, a credential, GitHub, the archive."""


@dataclass(frozen=True, slots=True)
class GitHubFolder:
    """A folder of a GitHub repository; ``ref`` ``None`` is the default branch."""

    owner: str
    repo: str
    ref: str | None = None
    path: str = ""


@dataclass(frozen=True, slots=True)
class BrandAssetSourceSync:
    """What one download read: the packs it adds, or why none was read.

    *shadowed* are packs of the source a bundled root already holds under the
    same name: the bundled one loads (a pack name loads once, the first root
    wins), so the source's is not used, and is not counted in *packs*.
    """

    packs: tuple[str, ...] = ()
    shadowed: tuple[str, ...] = ()
    error: str | None = None
    #: The (URL, integration id) the download read, when it got that far.
    setting: tuple[str, uuid.UUID | None] | None = None


def parse_github_folder_url(url: str) -> GitHubFolder:
    """``https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]`` as a :class:`GitHubFolder`.

    The ref is the segment after ``tree``, as GitHub's own links read: a branch
    whose name holds a ``/`` cannot be told from a path and is not supported.
    Anything else (another host, a file link, a query) is refused.
    """
    if len(url) > MAX_URL_LENGTH:
        raise BrandAssetSourceError(f"The URL is longer than {MAX_URL_LENGTH} characters.")
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise BrandAssetSourceError("The URL cannot be read.") from exc
    # GitHub's links percent-encode a folder or a ref ("Brand%20Assets"): the
    # archive names them decoded.
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in _GITHUB_WEB_HOSTS
        or parsed.port is not None
        or parsed.username
        or parsed.query
        or parsed.fragment
        or len(segments) < 2
        or (len(segments) > 2 and (segments[2] != "tree" or len(segments) < 4))
        or any(segment in (".", "..") or "/" in segment or "\\" in segment for segment in segments)
    ):
        raise BrandAssetSourceError(
            "Use a GitHub folder URL: https://github.com/OWNER/REPO, optionally followed by /tree/BRANCH_OR_TAG/FOLDER."
        )
    repo = segments[1].removesuffix(".git")
    if not repo:
        raise BrandAssetSourceError("The URL names no repository.")
    if len(segments) == 2:
        return GitHubFolder(owner=segments[0], repo=repo)
    return GitHubFolder(owner=segments[0], repo=repo, ref=segments[3], path="/".join(segments[4:]))


def zipball_url(folder: GitHubFolder) -> str:
    """The REST URL of the repository archive at the folder's ref."""
    base = f"{GITHUB_API}/repos/{quote(folder.owner, safe='')}/{quote(folder.repo, safe='')}/zipball"
    return f"{base}/{quote(folder.ref, safe='')}" if folder.ref else base


async def resolve_source_credential(db: AsyncSession, integration_id: uuid.UUID | str | None) -> SkillCredential:
    """The credential of the source's integration; one is required, the source being private."""
    if not integration_id:
        raise BrandAssetSourceError("No GitHub integration is selected for the brand asset source.")
    try:
        credential = await resolve_skill_credential(db, integration_id, subject=_SUBJECT, rebind_hint=_REBIND_HINT)
    except SkillCredentialError as exc:
        raise BrandAssetSourceError(str(exc)) from exc
    if credential is None:  # an id was given: never None
        raise BrandAssetSourceError("No GitHub integration is selected for the brand asset source.")
    return credential


async def download_archive(folder: GitHubFolder, credential: SkillCredential, target: Path) -> None:
    """Write the repository archive at the folder's ref to *target*, refused past :data:`MAX_ARCHIVE_BYTES`.

    Streamed to disk, never held in memory. GitHub answers with a redirect to
    a short-lived codeload URL; httpx drops the ``Authorization`` header when it
    follows a redirect to another host, so the token never leaves api.github.com.
    """
    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    try:
        async with egress_client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            with egress_route(credential.route):
                async with client.stream("GET", zipball_url(folder), headers=headers) as resp:
                    if resp.status_code >= 400:
                        raise BrandAssetSourceError(_failure_message(resp, folder, credential))
                    size = 0
                    with target.open("wb") as out:
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_ARCHIVE_BYTES:
                                raise BrandAssetSourceError(
                                    f"The archive of {folder.owner}/{folder.repo} is over "
                                    f"{MAX_ARCHIVE_BYTES // 1024 // 1024} MB: point the source at a "
                                    "smaller repository."
                                )
                            out.write(chunk)
    except httpx.HTTPError as exc:
        raise BrandAssetSourceError(f"GitHub could not be reached: {type(exc).__name__}.") from exc


def _failure_message(resp: httpx.Response, folder: GitHubFolder, credential: SkillCredential) -> str:
    where = f"{folder.owner}/{folder.repo}" + (f" at {folder.ref}" if folder.ref else "")
    kind = classify_github_failure(resp.status_code, resp.headers)
    if kind == "auth":
        return (
            f"GitHub rejected the token of integration '{credential.integration_name}' "
            f"(HTTP {resp.status_code}). Re-authorize it, or use a token that is still valid."
        )
    if kind == "rate_limit":
        return f"GitHub is throttling integration '{credential.integration_name}' (HTTP {resp.status_code})."
    if kind == "not_found":
        return (
            f"GitHub returned 404 for {where}: the repository or ref does not exist, or the "
            f"token of '{credential.integration_name}' cannot read it (it needs Contents: Read)."
        )
    return f"GitHub returned HTTP {resp.status_code} for {where}."


def extract_packs(archive_file: Path, path: str, dest: Path) -> tuple[str, ...]:
    """Write the packs under *path* of the GitHub archive *archive_file* into *dest*; the pack names.

    GitHub roots every entry of the archive in one ``<owner>-<repo>-<sha>/``
    folder, which is dropped. Only ``<path>/<pack>/<file>`` is written; a pack
    without a ``manifest.json`` is left out. A member declared above the seed
    ceiling is not extracted (the loader would skip it, and reports the
    entry). Synchronous and CPU-bound: run it off the event loop.
    """
    prefix = [segment for segment in path.split("/") if segment]
    budget = ArchiveBudget(MAX_EXTRACTED_BYTES)
    files: dict[str, list[str]] = {}
    written = 0
    try:
        with zipfile.ZipFile(archive_file) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = info.filename.split("/")[1:]
                if relative[: len(prefix)] != prefix:
                    continue
                relative = relative[len(prefix) :]
                if len(relative) != 2:
                    continue
                pack, name = relative
                if not PACK_NAME_RE.fullmatch(pack) or not name or name.startswith(".") or "\\" in name:
                    continue
                if info.file_size > MAX_SEED_ASSET_BYTES:
                    if name == "manifest.json":
                        # Without its manifest the pack would vanish, and the
                        # rows it fed would read as removed.
                        raise BrandAssetSourceError(f"The manifest of pack {pack} is over the size limit.")
                    continue
                written += 1
                if written > MAX_FILES:
                    raise BrandAssetSourceError(f"The folder holds more than {MAX_FILES} pack files.")
                data = budget.read(archive, info, max_member=MAX_SEED_ASSET_BYTES)
                target = dest / pack
                target.mkdir(exist_ok=True)
                (target / name).write_bytes(data)
                files.setdefault(pack, []).append(name)
    except (zipfile.BadZipFile, ArchiveTooLargeError) as exc:
        raise BrandAssetSourceError(f"The repository archive cannot be read: {exc}") from exc
    packs = tuple(sorted(pack for pack, names in files.items() if "manifest.json" in names))
    if not packs:
        where = "/".join(prefix) or "the repository root"
        raise BrandAssetSourceError(f"No brand pack (PACK/manifest.json) was found under {where}.")
    return packs


async def configured_source(db: AsyncSession) -> tuple[str | None, uuid.UUID | None]:
    """The URL and integration id the settings name; ``(None, None)`` when unset."""
    row = (await db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none()
    if row is None or not row.brand_assets_source_url:
        return None, None
    return row.brand_assets_source_url, row.brand_assets_source_integration_id


async def _read_configured_source() -> tuple[str | None, uuid.UUID | None]:
    from app.core.database import async_session

    async with async_session() as db:
        return await configured_source(db)


async def _source_and_credential() -> tuple[GitHubFolder, SkillCredential, tuple[str, uuid.UUID | None]] | None:
    """The configured folder, its credential and the setting read, in a session closed before any download."""
    from app.core.database import async_session

    async with async_session() as db:
        url, integration_id = await configured_source(db)
        if not url:
            return None
        credential = await resolve_source_credential(db, integration_id)
        return parse_github_folder_url(url), credential, (url, integration_id)


async def source_unchanged(sync: BrandAssetSourceSync) -> bool:
    """Whether the setting still names what *sync* read: a save may land while another sync downloads."""
    return sync.setting is None or await _read_configured_source() == sync.setting


def _bundled_pack_names() -> set[str]:
    from app.services.template_asset_seed import brand_asset_roots

    return {
        manifest.parent.name
        for root in brand_asset_roots()
        if root.is_dir()
        for manifest in root.glob("*/manifest.json")
    }


@asynccontextmanager
async def downloaded_source() -> AsyncIterator[tuple[Path | None, BrandAssetSourceSync | None]]:
    """Download the configured source; yield its packs' root and what was read.

    The sync is ``None`` when no source is configured, and so is the root
    when nothing was extracted. Nothing from the source may stop the caller:
    any failure (GitHub, the credential, a stored secret that no longer
    decrypts, the disk, :data:`DOWNLOAD_DEADLINE_SECONDS`) is logged and
    reported in the sync's ``error``. The root, a temporary folder created only
    for a configured source, goes when the block ends. Runs outside
    ``bootstrap_lock`` and outside any database session: only the seed that
    reads the root needs the lock.
    """
    tmp: tempfile.TemporaryDirectory[str] | None = None
    root: Path | None = None
    sync: BrandAssetSourceSync | None = None
    setting: tuple[str, uuid.UUID | None] | None = None
    try:
        source = await _source_and_credential()
        if source is not None:
            folder, credential, setting = source
            tmp = tempfile.TemporaryDirectory(prefix="brand-asset-source-")
            work = Path(tmp.name)
            archive = work / "archive.zip"
            async with asyncio.timeout(DOWNLOAD_DEADLINE_SECONDS):
                await download_archive(folder, credential, archive)
            packs_root = work / "packs"
            packs_root.mkdir()
            packs = await asyncio.to_thread(extract_packs, archive, folder.path, packs_root)
            archive.unlink()
            bundled = _bundled_pack_names()
            root = packs_root
            sync = BrandAssetSourceSync(
                packs=tuple(p for p in packs if p not in bundled),
                shadowed=tuple(p for p in packs if p in bundled),
                setting=setting,
            )
            logger.info("Brand asset source: read the pack(s) %s", ", ".join(packs))
            if sync.shadowed:
                logger.warning(
                    "Brand asset source: the pack(s) %s are also bundled; the bundled ones are used",
                    ", ".join(sync.shadowed),
                )
    except BrandAssetSourceError as exc:
        sync = BrandAssetSourceSync(error=str(exc), setting=setting)
        logger.warning("Brand asset source not read; the seed runs without it: %s", exc)
    except TimeoutError:
        sync = BrandAssetSourceSync(
            error=f"GitHub did not send the archive within {DOWNLOAD_DEADLINE_SECONDS} s.", setting=setting
        )
        logger.warning("Brand asset source not read in %d s; the seed runs without it", DOWNLOAD_DEADLINE_SECONDS)
    except Exception as exc:  # the seed, and the boot, must survive any failure of the source
        sync = BrandAssetSourceSync(
            error=f"The brand asset source could not be read ({type(exc).__name__}).", setting=setting
        )
        logger.warning("Brand asset source not read; the seed runs without it", exc_info=True)
    try:
        yield root, sync
    finally:
        if tmp is not None:
            tmp.cleanup()


async def seed_with_source(platform_mode: str, root: Path | None, sync: BrandAssetSourceSync | None) -> int:
    """Run the asset seed with what :func:`downloaded_source` read. Returns the uploads.

    A source that was not read retires nothing in any template: a pack of the
    source may feed a template directly (its own ``container``), and its seeded
    assets are missing, not removed. Callers hold ``bootstrap_lock()``, as for
    every seed.
    """
    from app.core.database import async_session
    from app.services.template_asset_seed import seed_builtin_template_assets

    read = sync is None or sync.error is None
    if not read:
        logger.warning("Brand asset source not read: every template keeps its seeded assets, none retired")
    async with async_session() as db:
        return await seed_builtin_template_assets(
            db,
            platform_mode=platform_mode,
            extra_roots=(root,) if root is not None and sync is not None and sync.packs else (),
            retire=read,
        )


async def seed_template_assets(platform_mode: str) -> tuple[int, BrandAssetSourceSync | None]:
    """Download the source, then run the asset seed with it. Returns (uploads, sync).

    For the boot, which already holds ``bootstrap_lock()``; the settings routes
    download first and take the lock for the seed only.
    """
    async with downloaded_source() as (root, sync):
        return await seed_with_source(platform_mode, root, sync), sync
