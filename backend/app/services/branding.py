# SPDX-License-Identifier: Apache-2.0

"""Platform branding resolver.

Loads admin-configured branding (name, logo, description, theme colors,
favicon, social preview) from the ``platform_settings`` row and exposes
both an async DB-aware resolver and a sync cached accessor.

Branding is intentionally decoupled from ``PLATFORM_MODE``.  The mode
still drives CSS theming, licensing, seeding, and security defaults,
but every user-visible text and asset must come through this resolver.

Usage::

    # In async request handlers:
    branding = await get_branding(db)
    name = branding.name

    # In sync contexts that cannot await or do not have a DB session:
    name = get_cached_branding().name

Cache contract:

* ``get_branding(db)`` is the source-of-truth async loader. It performs DB I/O
  and never mutates process globals.
* ``get_cached_branding()`` is a synchronous, in-process snapshot for code paths
  that cannot await. It starts with env/mode defaults, then is replaced after
  startup and after committed admin branding updates.
* ``refresh_branding_cache(db)`` awaits DB I/O first, then swaps one frozen
  ``Branding`` object reference. Readers therefore see either the previous
  committed snapshot or the next committed snapshot, never a temporary
  reset/default during refresh.
* The cache is per-process. Every uvicorn worker, replica and SAQ worker primes
  its own snapshot at startup and re-reads it in ``keep_branding_current``, so an
  admin change reaches every process within ``BRANDING_REFRESH_INTERVAL_SECONDS``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_path import BASE_PATH_META_NAME, with_base_path
from app.core.config import settings as app_settings
from app.core.csp import inline_script_hashes
from app.models.platform_settings import PlatformSettings

logger = logging.getLogger(__name__)

# How often every process re-reads the stored branding (one indexed SELECT).
BRANDING_REFRESH_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class BrandingDefaults:
    """Mode-specific defaults for branding fields not yet configured by an admin."""

    name: str
    logo: str
    # The same wordmark for a light surface: the bundled ``logo`` paints its
    # text white, for the dark theme, and vanishes on the light one.
    logo_light: str
    # The product mark without its wordmark, for the collapsed navigation
    # rail (a 28px square), and its light-theme variant.
    emblem: str
    emblem_light: str
    description: str
    theme_color: str
    accent_color: str
    background_color: str
    og_image: str
    # Login-screen subtitle shown under the logo on the login page.
    login_message: str


_XTM_ONE_DEFAULTS = BrandingDefaults(
    name="XTM One",
    logo="/xtp-one.svg",
    logo_light="/xtp-one-light.svg",
    # The design system's XTM One mark colours: purple-300 on dark, purple-500 on light.
    emblem="/xtm-one-emblem.svg",
    emblem_light="/xtm-one-emblem-light.svg",
    description="Unified Agentic AI for Continuous Threat Exposure Management",
    theme_color="#5E1AD5",
    accent_color="#8c3dff",
    background_color="#070d19",
    og_image="/xtp-one-icon.svg",
    login_message="Autonomous AI agents for threat exposure management",
)

_COPILOT_DEFAULTS = BrandingDefaults(
    name="Filigran Copilot",
    logo="/filigran-copilot.svg",
    logo_light="/filigran-copilot-light.svg",
    emblem="/filigran-white.svg",
    emblem_light="/filigran-black.svg",
    description="Next-gen enterprise agentic AI platform by Filigran",
    theme_color="#0a0f1c",
    accent_color="#0fbcff",
    background_color="#070d19",
    og_image="/og-image.png",
    login_message="Your open agentic AI platform",
)


def branding_defaults(is_xtm: bool) -> BrandingDefaults:
    """Return the mode-specific branding defaults."""
    return _XTM_ONE_DEFAULTS if is_xtm else _COPILOT_DEFAULTS


@dataclass(frozen=True)
class Branding:
    """Resolved platform branding fields.

    All fields except ``name`` may be ``None`` — callers should fall back
    to bundled defaults for any missing asset.
    """

    name: str
    logo: str | None = None
    description: str | None = None
    theme_color: str | None = None
    accent_color: str | None = None
    background_color: str | None = None
    favicon: str | None = None
    og_image: str | None = None
    # Markdown login-screen message. ``None`` falls back to the mode default.
    login_message: str | None = None
    # Stable per-install identifier (the singleton PlatformSettings row
    # UUID).  Exposed in the admin Settings page as "Instance ID" and
    # used by license/observability code paths that need an opaque
    # deployment key.  ``None`` only before the DB row has been seeded.
    instance_id: str | None = None
    # The platform default UI language (``PlatformSettings.platform_language``,
    # ``auto`` when unset): what a channel notice to a sender with no language
    # of their own is written in (``services/channel_notices.py``).
    language: str | None = None


@dataclass(frozen=True)
class BrandingAsset:
    """Decoded branding asset data ready for API routes or other callers."""

    mime: str
    data: bytes
    etag: str


SAFE_BRANDING_IMAGE_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "image/gif",
    }
)

BRANDING_IMAGE_MAX_BYTES: dict[str, int] = {
    "logo": 512 * 1024,
    "favicon": 64 * 1024,
    "og-image": 512 * 1024,
}

BRANDING_ASSET_KIND_TO_FIELD: dict[str, str] = {
    "logo": "logo",
    "favicon": "favicon",
    "og-image": "og_image",
}


def mode_default_name() -> str:
    """Return the mode-aware fallback product name when DB branding is unset."""
    return "XTM One" if app_settings.is_xtm_one else "Filigran Copilot"


def _env_name() -> str | None:
    """Return the optional env-var override for the product name."""
    value = (app_settings.platform_name or "").strip()
    return value or None


def _env_logo() -> str | None:
    """Return the optional env-var override for the product logo (base64 data URL)."""
    value = (app_settings.platform_logo or "").strip()
    return _safe_branding_data_url(value, BRANDING_IMAGE_MAX_BYTES["logo"]) if value else None


def _resolved_name(db_value: str | None) -> str:
    """Resolve the product name with DB > env > mode-default precedence."""
    return db_value or _env_name() or mode_default_name()


def _safe_logo_value(value: str | None) -> str | None:
    if not value:
        return None
    # ``platform_logo_url`` is the deprecated absolute-URL fallback; only
    # data URLs need MIME filtering here.
    if not value.startswith("data:"):
        return value
    return _safe_branding_data_url(value, BRANDING_IMAGE_MAX_BYTES["logo"])


def _resolved_logo(db_value: str | None) -> str | None:
    """Resolve the product logo with DB > env > None precedence."""
    safe = _safe_logo_value(db_value)
    if safe is not None:
        return safe
    return _env_logo()


def _env_default_branding() -> Branding:
    """Branding used when no DB row exists yet — applies env-var overrides."""
    return Branding(name=_resolved_name(None), logo=_resolved_logo(None))


_cached_branding: Branding


def _from_row(ps: PlatformSettings | None) -> Branding:
    if ps is None:
        return _env_default_branding()
    return Branding(
        name=_resolved_name(ps.platform_name),
        # Prefer the new base64 column; fall back to the deprecated URL column;
        # then to the env-var override.
        logo=_safe_logo_value(ps.platform_logo)
        or _safe_logo_value(ps.platform_logo_url)
        or _env_logo(),
        description=ps.platform_description,
        theme_color=ps.platform_theme_color,
        accent_color=ps.platform_accent_color,
        background_color=ps.platform_background_color,
        favicon=_safe_branding_data_url(
            ps.platform_favicon,
            BRANDING_IMAGE_MAX_BYTES["favicon"],
        ),
        og_image=_safe_branding_image_value(
            ps.platform_og_image,
            allow_url=True,
            max_bytes=BRANDING_IMAGE_MAX_BYTES["og-image"],
        ),
        # ``getattr`` (not direct attribute access) keeps the resolver
        # tolerant of lightweight test stubs that mimic PlatformSettings
        # without declaring every column.
        login_message=getattr(ps, "platform_login_message", None),
        instance_id=(
            str(getattr(ps, "id", None))
            if getattr(ps, "id", None) is not None
            else None
        ),
        language=getattr(ps, "platform_language", None),
    )


def estimated_base64_decoded_size(payload: str) -> int:
    """Return the decoded byte count implied by a base64 payload length."""
    padding = 2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
    return max(0, (len(payload) * 3) // 4 - padding)


def parse_branding_data_url(value: str) -> tuple[str, str] | None:
    prefix, sep, payload = value.partition(",")
    if sep != ",":
        return None
    if not prefix.lower().startswith("data:") or not prefix.lower().endswith(";base64"):
        return None
    mime = prefix[5:-7].strip().lower()
    if not mime:
        return None
    return mime, payload


def _safe_branding_data_url(
    value: str | None,
    max_bytes: int | None = None,
    allowed_mimes: frozenset[str] = SAFE_BRANDING_IMAGE_MIMES,
) -> str | None:
    if not value:
        return None
    parsed = parse_branding_data_url(value)
    if parsed is None:
        return None
    mime, payload = parsed
    if mime not in allowed_mimes:
        return None
    if max_bytes is not None and estimated_base64_decoded_size(payload) > max_bytes:
        return None
    return value


def _safe_branding_image_value(
    value: str | None,
    *,
    allow_url: bool = False,
    max_bytes: int | None = None,
) -> str | None:
    if not value:
        return None
    if allow_url and value.startswith(("http://", "https://")):
        return value
    return _safe_branding_data_url(value, max_bytes)


_cached_branding = _env_default_branding()
_index_html_content = ""
_index_html_script_hashes: tuple[str, ...] = ()
_index_html_frontend_dir: Path | None = None
# The branding the served index.html was last rendered with.
_index_html_branding: Branding | None = None


def decode_branding_data_url(
    value: str,
    *,
    allowed_mimes: frozenset[str] | None = None,
    max_bytes: int | None = None,
) -> tuple[str, bytes] | None:
    """Return ``(mime, raw_bytes)`` for a data URL, or ``None`` otherwise."""
    parsed = parse_branding_data_url(value)
    if parsed is None:
        return None
    mime, payload = parsed
    if allowed_mimes is not None and mime not in allowed_mimes:
        return None
    if max_bytes is not None and estimated_base64_decoded_size(payload) > max_bytes:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    if max_bytes is not None and len(data) > max_bytes:
        return None
    return mime, data


def _data_url_hash(value: str | None) -> str | None:
    """Return a short stable hash for a ``data:...;base64,...`` branding value.

    Hashes the base64 payload string directly (no decode) so callers can
    cheaply derive a cache-busting version on every request — the actual
    bytes are only decoded once, on demand, by the binary asset endpoint.
    Returns ``None`` for anything that isn't a data URL (unset, legacy
    absolute URL, or bundled static asset path).

    Note this is a *different* hash than ``BrandingAsset.etag`` (computed by
    ``resolve_branding_asset()`` over the decoded raw bytes): both are stable
    fingerprints of the same underlying image and both invalidate on content
    change, but this one deliberately skips the base64 decode since it only
    needs to be a cheap ``?v=`` cache-buster, not a byte-exact validator.
    """
    if not value or not value.startswith("data:"):
        return None
    parsed = parse_branding_data_url(value)
    if parsed is None:
        return None
    _, payload = parsed
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resolved_branding_payload(branding: Branding, *, is_xtm: bool) -> dict[str, str | None]:
    """Return API-ready branding fields with mode defaults applied.

    Uploaded images (``data:...;base64,...`` values) are never inlined here —
    doing so bloated every ``GET /platform/config`` response by up to ~1.4 MB
    on a route fetched on every page load. Instead this returns the binary
    asset endpoint path (``/api/v1/platform/branding/asset/{kind}``) plus a
    short content hash (``platform_*_hash``) the frontend appends as a
    ``?v=`` cache-buster, so the JSON payload stays tiny and the browser
    caches the actual bytes after the first load.
    """
    d = branding_defaults(is_xtm=is_xtm)
    logo_hash = _data_url_hash(branding.logo)
    favicon_hash = _data_url_hash(branding.favicon)
    og_image_hash = _data_url_hash(branding.og_image)
    own_logo = bool(logo_hash or branding.logo)
    favicon_path = "/api/v1/platform/branding/asset/favicon" if favicon_hash else None
    return {
        "platform_name": branding.name or d.name,
        "platform_logo": (
            "/api/v1/platform/branding/asset/logo"
            if logo_hash
            else branding.logo or d.logo
        ),
        "platform_logo_hash": logo_hash,
        # Only while the bundled logo is in use: an operator's own logo (an
        # upload or a URL) is theirs to draw in both themes.
        "platform_logo_light": None if own_logo else d.logo_light,
        # The mark the collapsed rail shows. Beside an operator's own logo the
        # bundled emblem would be another product's: their uploaded favicon is
        # the square mark they chose, and with none the client crops the logo.
        "platform_emblem": favicon_path if own_logo else d.emblem,
        "platform_emblem_hash": favicon_hash if own_logo else None,
        "platform_emblem_light": None if own_logo else d.emblem_light,
        "platform_description": branding.description or d.description,
        "platform_theme_color": branding.theme_color or d.theme_color,
        "platform_favicon": favicon_path,
        "platform_favicon_hash": favicon_hash,
        "platform_accent_color": branding.accent_color,
        "platform_background_color": branding.background_color,
        "platform_default_accent_color": d.accent_color,
        "platform_default_background_color": d.background_color,
        "platform_og_image": (
            "/api/v1/platform/branding/asset/og-image"
            if og_image_hash
            else branding.og_image or d.og_image
        ),
        "platform_og_image_hash": og_image_hash,
        "platform_login_message": branding.login_message or d.login_message,
    }


def resolve_branding_asset(branding: Branding, kind: str) -> BrandingAsset | None:
    """Return decoded asset data for ``kind``, or ``None`` when unavailable."""
    attr = BRANDING_ASSET_KIND_TO_FIELD.get(kind)
    if attr is None:
        return None

    value = getattr(branding, attr, None)
    if value is None:
        return None

    decoded = decode_branding_data_url(
        value,
        allowed_mimes=SAFE_BRANDING_IMAGE_MIMES,
        max_bytes=BRANDING_IMAGE_MAX_BYTES.get(kind),
    )
    if decoded is None:
        return None

    mime, raw = decoded
    return BrandingAsset(mime=mime, data=raw, etag=f'"{hashlib.sha256(raw).hexdigest()}"')


def branding_asset_versions(branding: Branding, kind: str, *, is_xtm: bool) -> frozenset[str]:
    """The ``?v=`` values the platform writes today for the asset of ``kind``.

    Two spellings name the same bytes: the asset's content hash (the
    ``platform_*_hash`` the frontend appends, the manifest icons, the
    ``og:image``) and the head version the branded ``index.html`` puts on its
    favicon links, a hash over every branding field, the asset included. The
    asset endpoint keeps its long lifetime for a URL carrying one of them; any
    other URL (the bare path, a version an earlier upload wrote) names no
    particular bytes and is revalidated instead.
    """
    attr = BRANDING_ASSET_KIND_TO_FIELD.get(kind)
    content_hash = _data_url_hash(getattr(branding, attr, None) if attr else None)
    if content_hash is None:
        return frozenset()
    payload = resolved_branding_payload(branding, is_xtm=is_xtm)
    return frozenset({content_hash, _branding_head_version(branding, payload, is_xtm=is_xtm)})


def build_web_manifest(
    branding: Branding, *, is_xtm: bool, base_path: str | None = None
) -> dict:
    """Return a Web App Manifest reflecting current branding settings.

    Every URL in a manifest is resolved against the MANIFEST's own URL, which
    lives under ``/api/v1/platform/`` - so ``start_url``, ``scope`` and every
    ``icons[].src`` are written root-relative WITH the platform's URL path
    prefix (``base_path``, defaulting to the configured ``BASE_PATH``)
    rather than relative to the manifest.
    """
    d = branding_defaults(is_xtm=is_xtm)
    if base_path is None:
        base_path = app_settings.base_path

    def _url(path: str) -> str:
        return with_base_path(path, base_path)

    icons: list[dict[str, str]] = []
    if branding.favicon is not None:
        decoded = decode_branding_data_url(
            branding.favicon,
            allowed_mimes=SAFE_BRANDING_IMAGE_MIMES,
            max_bytes=BRANDING_IMAGE_MAX_BYTES["favicon"],
        )
        if decoded is not None:
            # ``?v=`` content hash on the icon src: the asset endpoint keeps
            # ``Cache-Control: public, max-age=86400`` for a URL whose ``v``
            # names the bytes it serves (``branding_asset_versions``) and
            # revalidates any other, so the versioned src is what lets
            # browsers cache the icon without showing a replaced one.
            icons.append(
                {
                    "src": _url(
                        "/api/v1/platform/branding/asset/favicon"
                        f"?v={_data_url_hash(branding.favicon)}"
                    ),
                    "sizes": "any",
                    "type": decoded[0],
                }
            )
    if branding.logo is not None and branding.logo.startswith("data:"):
        decoded = decode_branding_data_url(
            branding.logo,
            allowed_mimes=SAFE_BRANDING_IMAGE_MIMES,
            max_bytes=BRANDING_IMAGE_MAX_BYTES["logo"],
        )
        if decoded is not None:
            icons.append(
                {
                    "src": _url(
                        "/api/v1/platform/branding/asset/logo"
                        f"?v={_data_url_hash(branding.logo)}"
                    ),
                    "sizes": "any",
                    "type": decoded[0],
                    "purpose": "any maskable",
                }
            )
    if not icons:
        # The PNGs at 192 and 512 px (and a maskable one with the mark inside
        # the safe zone) are what a phone needs to offer "Install app": the
        # installed web app is the mobile companion (``components/companion``).
        if is_xtm:
            icons = [
                {"src": _url("/xtp-one-icon.svg"), "sizes": "any", "type": "image/svg+xml"},
                {"src": _url("/xtm-one-icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": _url("/xtm-one-icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {
                    "src": _url("/xtm-one-icon-maskable-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ]
        else:
            icons = [
                {"src": _url("/favicon-16x16.png"), "sizes": "16x16", "type": "image/png"},
                {"src": _url("/favicon-32x32.png"), "sizes": "32x32", "type": "image/png"},
                {"src": _url("/apple-touch-icon.png"), "sizes": "180x180", "type": "image/png"},
                {"src": _url("/copilot-icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": _url("/copilot-icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {
                    "src": _url("/copilot-icon-maskable-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ]

    return {
        # A stable identity: the installed app stays the same app when the
        # start URL or the branding changes.
        "id": f"{base_path}/",
        "name": branding.name,
        "short_name": branding.name,
        "description": branding.description or d.description,
        "start_url": f"{base_path}/",
        "scope": f"{base_path}/",
        "display": "standalone",
        "theme_color": branding.theme_color or d.theme_color,
        "background_color": branding.background_color or d.theme_color,
        "icons": icons,
        # The companion's tabs, offered on a long press of the app icon.
        "shortcuts": [
            {"name": "Today", "url": f"{base_path}/"},
            {"name": "Inbox", "url": f"{base_path}/inbox"},
            {"name": "Ask", "url": f"{base_path}/chat"},
        ],
        # "Share to" from any app (Android): the service worker keeps what is
        # shared until the app's Share page puts it in a new conversation,
        # unsent (``frontend/public/sw.js``).
        "share_target": {
            "action": f"{base_path}/share",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [{"name": "files", "accept": ["image/*", "application/pdf", "text/plain", "text/csv"]}],
            },
        },
    }


def _branding_head_version(
    branding: Branding,
    payload: dict[str, str | None],
    *,
    is_xtm: bool,
) -> str:
    value = "\0".join(
        [
            payload.get("platform_name") or "",
            payload.get("platform_description") or "",
            payload.get("platform_theme_color") or "",
            branding.favicon or "",
            # Use the raw (content-varying) favicon / logo / og_image values,
            # not the payload's resolved asset paths — those paths are stable
            # across content changes now that uploaded images are served from
            # a dedicated binary endpoint instead of being inlined as base64.
            # The logo and background color both feed the web manifest
            # (icons[] / background_color), so they must bust the manifest
            # ``?v=`` cache-buster too.
            branding.logo or "",
            branding.background_color or "",
            branding.og_image or "",
            "xtm" if is_xtm else "copilot",
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _branded_head_links(
    branding: Branding,
    payload: dict[str, str | None],
    *,
    is_xtm: bool,
    base_path: str = "",
) -> str:
    version = _branding_head_version(branding, payload, is_xtm=is_xtm)
    if branding.favicon is not None:
        favicon_href = f"/api/v1/platform/branding/asset/favicon?v={version}"
        apple_touch_href = favicon_href
    elif is_xtm:
        favicon_href = "/xtp-one-icon.svg"
        apple_touch_href = "/xtp-one-icon.svg"
    else:
        favicon_href = "/favicon.ico"
        apple_touch_href = "/apple-touch-icon.png"
    manifest_href = f"/api/v1/platform/manifest.webmanifest?v={version}"

    links = [
        (
            f'    <link rel="icon" href="{html_escape(with_base_path(favicon_href, base_path))}"'
            ' sizes="any" />'
        ),
        (
            f'    <link rel="apple-touch-icon" sizes="180x180"'
            f' href="{html_escape(with_base_path(apple_touch_href, base_path))}" />'
        ),
        (
            '    <link rel="manifest"'
            f' href="{html_escape(with_base_path(manifest_href, base_path))}" />'
        ),
    ]
    return "\n".join(links)


# ``src`` / ``href`` attribute values the bundler wrote relative to the
# document (``./assets/index-abc.js`` - Vite builds with a relative ``base`` so
# the bundle's OWN asset references resolve from the bundle wherever it is
# served) or root-relative (``/favicon.ico``). Never ``//host/...``.
_DOCUMENT_URL_ATTR_RE = re.compile(r'(\s(?:src|href)=")(?:\./|/)(?!/)')


def rebase_document_urls(html: str, base_path: str) -> str:
    """Make every bundle / asset URL in the built ``index.html`` absolute.

    ``index.html`` is the ONE file whose URLs must not be relative: it is served
    for every SPA route, so a relative ``./assets/index.js`` on a hard load of
    ``/agents/123`` would resolve to ``/agents/assets/index.js`` and the page
    would never boot. Rewritten to ``{base_path}/assets/index.js`` - the public
    URL under the platform's path prefix, or ``/assets/index.js`` at
    the root. The bundle's own references (code-split chunks, fonts, images)
    are relative to the bundle and need nothing.
    """
    # Written into an attribute value: escaped like every other value this
    # renderer writes, even though the validator only lets unreserved
    # characters through.
    prefix = html_escape(base_path)
    return _DOCUMENT_URL_ATTR_RE.sub(lambda m: f"{m.group(1)}{prefix}/", html)


#: ``<meta name>`` the branded ``index.html`` carries its frontend build in.
#: Read by ``frontend/src/lib/build-version.ts``: a page compares the build it
#: booted with to the one ``GET /auth/config`` reports and offers a reload
#: when the server moved on.
BUILD_ID_META_NAME = "build-id"
_BUILD_ID_META_RE = re.compile(
    rf'<meta\s+name="{re.escape(BUILD_ID_META_NAME)}"\s+content="([0-9a-f]*)"',
    re.IGNORECASE,
)


def frontend_build_id(source_html: str) -> str:
    """The id of the frontend build a built ``index.html`` belongs to.

    A digest of the file as the bundler wrote it: its script and stylesheet
    names carry the content hashes of the whole bundle, so every build that
    changes a line of the frontend changes the id, and rebuilding the same
    code keeps it.
    """
    return hashlib.sha256(source_html.encode("utf-8")).hexdigest()[:16]


def get_frontend_build_id() -> str:
    """The build id of the frontend this process serves (``""`` without one).

    Read back from the served ``index.html`` itself, so what ``/auth/config``
    reports is exactly what a page loaded from this process booted with.
    """
    match = _BUILD_ID_META_RE.search(_index_html_content)
    return match.group(1) if match else ""


def _inject_build_id_meta(html: str, build_id: str) -> str:
    """Write the build id right after the base-path meta, replacing any copy."""
    html = re.sub(
        rf'\s*<meta\s+name="{re.escape(BUILD_ID_META_NAME)}"[^>]*>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    tag = f'<meta name="{BUILD_ID_META_NAME}" content="{html_escape(build_id)}" />'
    return re.sub(
        rf'(<meta\s+name="{re.escape(BASE_PATH_META_NAME)}"[^>]*>)',
        lambda m: f"{m.group(1)}\n    {tag}",
        html,
        count=1,
        flags=re.IGNORECASE,
    )


def _base_path_meta_tag(base_path: str) -> str:
    return f'<meta name="{BASE_PATH_META_NAME}" content="{html_escape(base_path)}" />'


def _inject_base_path_meta(html: str, base_path: str) -> str:
    """Tell the SPA its URL path prefix before its first request.

    Always written - an empty ``content`` reads as the root - right after the
    charset declaration (or, failing that, at the end of ``<head>``), replacing
    any earlier copy so a re-render never stacks two.
    """
    html = re.sub(
        rf'\s*<meta\s+name="{re.escape(BASE_PATH_META_NAME)}"[^>]*>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    tag = _base_path_meta_tag(base_path)
    if re.search(r"<meta\s+charset=[^>]*>", html, flags=re.IGNORECASE):
        return re.sub(
            r"(<meta\s+charset=[^>]*>)",
            lambda m: f"{m.group(1)}\n    {tag}",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    return re.sub(
        r"\s*</head>",
        f"\n    {tag}\n  </head>",
        html,
        count=1,
        flags=re.IGNORECASE,
    )


def render_branded_index_html(
    html: str,
    *,
    branding: Branding | None = None,
    is_xtm: bool | None = None,
    base_path: str | None = None,
) -> str:
    """Patch built frontend HTML metadata with current branding.

    Also anchors the document to the platform's URL path prefix:
    bundle and asset URLs become absolute under ``base_path`` (defaulting to
    the configured ``BASE_PATH``) and a ``<meta name="base-path">`` carries the
    prefix to the SPA. Runs BEFORE the head links are written, which arrive
    already prefixed, so nothing is prefixed twice. A
    ``<meta name="build-id">`` names the build of the SOURCE document
    (:func:`frontend_build_id`), stable across every re-render.
    """
    b = branding or get_cached_branding()
    mode_is_xtm = app_settings.is_xtm_one if is_xtm is None else is_xtm
    prefix = app_settings.base_path if base_path is None else base_path
    payload = resolved_branding_payload(b, is_xtm=mode_is_xtm)
    build_id = frontend_build_id(html)

    html = _inject_base_path_meta(rebase_document_urls(html, prefix), prefix)
    html = _inject_build_id_meta(html, build_id)

    def _sub(pattern: str, repl) -> None:
        nonlocal html
        html = re.sub(pattern, repl, html, count=1, flags=re.IGNORECASE)

    _sub(r"<title>[^<]*</title>", lambda _m: f"<title>{html_escape(b.name)}</title>")
    _sub(
        r'(<meta\s+property="og:title"\s+content=)"[^"]*"',
        lambda m: f'{m.group(1)}"{html_escape(b.name)}"',
    )
    _sub(
        r'(<meta\s+name="description"\s+content=)"[^"]*"',
        lambda m: f'{m.group(1)}"{html_escape(payload["platform_description"] or "")}"',
    )
    _sub(
        r'(<meta\s+property="og:description"\s+content=)"[^"]*"',
        lambda m: f'{m.group(1)}"{html_escape(payload["platform_description"] or "")}"',
    )
    _sub(
        r'(<meta\s+name="theme-color"\s+content=)"[^"]*"',
        lambda m: f'{m.group(1)}"{html_escape(payload["platform_theme_color"] or "")}"',
    )
    # Mirror the frontend's ``withVersion()``: when the OG image resolves to
    # the binary asset endpoint, append the content hash so social crawlers
    # don't keep a day-stale cached image after an admin update.
    og_image_href = payload["platform_og_image"] or ""
    og_image_hash = payload.get("platform_og_image_hash")
    if og_image_href and og_image_hash:
        og_image_href = f"{og_image_href}?v={og_image_hash}"
    # Crawlers resolve nothing through the SPA, so the prefix goes on here
    # (an absolute OG image URL is left as the admin wrote it).
    og_image_href = with_base_path(og_image_href, prefix)
    _sub(
        r'(<meta\s+property="og:image"\s+content=)"[^"]*"',
        lambda m: f'{m.group(1)}"{html_escape(og_image_href)}"',
    )
    head_links = _branded_head_links(b, payload, is_xtm=mode_is_xtm, base_path=prefix)
    html = re.sub(
        r'\s*<link\b(?=[^>]*\brel="(?:icon|apple-touch-icon|mask-icon|manifest)")'
        r"[^>]*>\s*",
        "\n",
        html,
        flags=re.IGNORECASE,
    )
    if re.search(r'\s*<link\s+rel="preconnect"', html, flags=re.IGNORECASE):
        html = re.sub(
            r'(\s*<link\s+rel="preconnect")',
            f"\n{head_links}\\1",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        html = re.sub(
            r"\s*</head>",
            f"\n{head_links}\n  </head>",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    return html


def refresh_branded_index_html(frontend_dir: Path) -> str:
    """Read and patch ``index.html`` from a built frontend directory."""
    if not frontend_dir.is_dir():
        return ""
    path = frontend_dir / "index.html"
    if not path.is_file():
        return ""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return render_branded_index_html(html)


def configure_branded_index_html(frontend_dir: Path) -> str:
    """Set the frontend directory used by the cached branded index renderer."""
    global _index_html_frontend_dir
    _index_html_frontend_dir = frontend_dir
    return refresh_configured_branded_index_html()


def refresh_configured_branded_index_html() -> str:
    """Refresh the cached branded index HTML when a frontend directory is configured."""
    global _index_html_content, _index_html_branding, _index_html_script_hashes
    if _index_html_frontend_dir is None:
        return _index_html_content
    rendered_with = _cached_branding
    refreshed = refresh_branded_index_html(_index_html_frontend_dir)
    if refreshed:
        _index_html_content = refreshed
        _index_html_script_hashes = inline_script_hashes(refreshed)
        _index_html_branding = rendered_with
    return _index_html_content


def index_html_is_current() -> bool:
    """True when the served index.html carries the cached branding.

    A failed render keeps the previous HTML silently; this is how a caller
    knows to retry. With no built frontend there is nothing to serve, so it is
    current by definition.
    """
    if _index_html_frontend_dir is None or not (_index_html_frontend_dir / "index.html").is_file():
        return True
    return _index_html_branding == _cached_branding


def get_branded_index_html() -> str:
    """Return the cached branded frontend index HTML."""
    return _index_html_content


def get_branded_index_script_hashes() -> tuple[str, ...]:
    """CSP hash sources of the inline scripts of the cached branded ``index.html``.

    Hashed from the rendered document: the renderer HTML-escapes every value
    it writes, so no script element can come from branding data.
    """
    return _index_html_script_hashes


async def get_branding(db: AsyncSession) -> Branding:
    """Load branding from the DB without mutating the sync cache."""
    result = await db.execute(select(PlatformSettings).limit(1))
    ps = result.scalar_one_or_none()
    return _from_row(ps)


# Reads apply in the order they started: a read that began before another one
# already applied (the refresh loop's, overtaken by an admin save's) returns
# older data and must not replace it.
_branding_reads_started = 0
_branding_read_applied = 0


async def refresh_branding_cache(db: AsyncSession) -> Branding:
    """Load branding from the DB and replace the sync cache; return the cached snapshot."""
    global _cached_branding, _branding_reads_started, _branding_read_applied
    _branding_reads_started += 1
    read = _branding_reads_started
    branding = await get_branding(db)
    if read > _branding_read_applied:
        _cached_branding = branding
        _branding_read_applied = read
    return _cached_branding


async def keep_branding_current(render: Callable[[], None] | None = None) -> None:
    """Keep this process's branding snapshot in step with the database, forever.

    An admin save refreshes only the process that served it; every other
    process re-reads the stored branding here. It only reads, so nothing needs
    coordinating. ``render`` re-renders what the process derives from the
    snapshot (the web processes: the OpenAPI title and the patched index.html).
    A change counts as applied only once that render succeeded and the served
    index.html carries the new snapshot, so a failed render is retried on the
    next cycle instead of being hidden by it. A failure is logged once per
    streak, and its end once.
    """
    from app.core.database import async_session

    rendered = _cached_branding
    failing = False
    while True:
        await asyncio.sleep(BRANDING_REFRESH_INTERVAL_SECONDS)
        try:
            async with async_session() as db:
                current = await refresh_branding_cache(db)
            if render is not None and (current != rendered or not index_html_is_current()):
                render()
                if not index_html_is_current():
                    raise RuntimeError("index.html kept its previous branding (the render could not read it)")
                rendered = current
                logger.info("Branding changed in the database: this process's caches were refreshed")
        except Exception:
            if not failing:
                logger.warning(
                    "Branding refresh failed; retrying every %ds", BRANDING_REFRESH_INTERVAL_SECONDS, exc_info=True
                )
            failing = True
            continue
        if failing:
            logger.info("Branding refresh recovered")
        failing = False


def get_cached_branding() -> Branding:
    """Return the last-loaded branding (sync).

    Falls back to the mode-aware product name if the cache has never been
    primed. Safe to call before startup; callers always get a usable
    ``Branding``.
    """
    return _cached_branding
