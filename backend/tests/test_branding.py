"""Tests for the platform branding resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import branding as branding_mod
from app.services.branding import (
    Branding,
    _branding_head_version,
    _data_url_hash,
    _from_row,
    branding_defaults,
    build_web_manifest,
    get_branding,
    get_cached_branding,
    mode_default_name,
    refresh_branding_cache,
    render_branded_index_html,
    resolved_branding_payload,
)


class _PSStub:
    """In-memory stand-in for a ``PlatformSettings`` row."""

    def __init__(self, **kwargs):
        defaults = dict(
            platform_name=None,
            platform_logo=None,
            platform_logo_url=None,
            platform_description=None,
            platform_login_message=None,
            platform_theme_color=None,
            platform_accent_color=None,
            platform_background_color=None,
            platform_favicon=None,
            platform_og_image=None,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class _ResultStub:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _DBStub:
    def __init__(self, row):
        self.row = row

    async def execute(self, _stmt):
        return _ResultStub(self.row)


# ── logo priority: base64 > legacy URL > None ────────────────────────────────

@pytest.mark.parametrize(
    ("platform_logo", "platform_logo_url", "expected"),
    [
        (
            "data:image/png;base64,AAA",
            "https://example.com/logo.png",
            "data:image/png;base64,AAA",
        ),
        (None, "https://example.com/logo.png", "https://example.com/logo.png"),
        (None, None, None),
    ],
    ids=["base64_wins", "url_fallback", "no_logo"],
)
def test_from_row_logo_priority(platform_logo, platform_logo_url, expected, env_branding):
    env_branding()
    ps = _PSStub(
        platform_name="Acme",
        platform_logo=platform_logo,
        platform_logo_url=platform_logo_url,
    )
    assert _from_row(ps).logo == expected


# ── name: DB value wins; None/blank row falls back to mode default ────────────

@pytest.mark.parametrize(
    "row",
    [None, _PSStub(platform_name=None), _PSStub(platform_name="")],
    ids=["no_row", "null_name", "blank_name"],
)
def test_from_row_name_falls_back_to_mode_default(row):
    assert _from_row(row).name == mode_default_name()


def test_from_row_name_uses_db_value():
    assert _from_row(_PSStub(platform_name="Acme Corp")).name == "Acme Corp"


# ── full field mapping ────────────────────────────────────────────────────────

def test_from_row_maps_all_fields():
    ps = _PSStub(
        platform_name="Acme",
        platform_description="The best platform",
        platform_login_message="Welcome to **Acme**",
        platform_theme_color="#1a2b3c",
        platform_favicon="data:image/x-icon;base64,AAA",
        platform_og_image="data:image/png;base64,BBB",
    )
    b = _from_row(ps)
    assert b.description == ps.platform_description
    assert b.login_message == ps.platform_login_message
    assert b.theme_color == ps.platform_theme_color
    assert b.favicon == ps.platform_favicon
    assert b.og_image == ps.platform_og_image


# ── login_message: row value maps through; mode default applied when unset ───
#
# This field is rendered on the public (unauthenticated) login page, so a
# regression here is immediately user-visible. ``_from_row`` carries the raw
# row value (``None`` when unset / on a stub that predates the column), and
# ``resolved_branding_payload`` is where the mode-aware default is applied.

def test_from_row_maps_login_message():
    ps = _PSStub(platform_login_message="See our [policy](https://x/p)")
    assert _from_row(ps).login_message == "See our [policy](https://x/p)"


@pytest.mark.parametrize(
    "row",
    [None, _PSStub(platform_login_message=None)],
    ids=["no_row", "null_login_message"],
)
def test_from_row_login_message_none_when_unset(row):
    assert _from_row(row).login_message is None


@pytest.mark.parametrize("is_xtm", [True, False], ids=["xtm", "copilot"])
def test_resolved_payload_uses_login_message_override(is_xtm):
    payload = resolved_branding_payload(
        Branding(name="Acme", login_message="Custom subtitle"),
        is_xtm=is_xtm,
    )
    assert payload["platform_login_message"] == "Custom subtitle"


@pytest.mark.parametrize(
    ("is_xtm", "login_message"),
    [(True, None), (True, ""), (False, None), (False, "")],
    ids=["xtm_null", "xtm_blank", "copilot_null", "copilot_blank"],
)
def test_resolved_payload_falls_back_to_mode_default(is_xtm, login_message):
    payload = resolved_branding_payload(
        Branding(name="Acme", login_message=login_message),
        is_xtm=is_xtm,
    )
    assert payload["platform_login_message"] == branding_defaults(is_xtm=is_xtm).login_message


# ── resolved_branding_payload: uploaded images are never inlined as base64 ───
#
# Regression coverage: GET /platform/config previously shipped up to ~1.4 MB
# of base64 image data on every page load. Uploaded images must now resolve
# to the binary asset endpoint path plus a short content hash, never a raw
# ``data:...;base64,...`` value.

def test_resolved_payload_never_inlines_base64_images():
    payload = resolved_branding_payload(
        Branding(
            name="Acme",
            logo="data:image/png;base64,QUFB",
            favicon="data:image/x-icon;base64,QkJC",
            og_image="data:image/png;base64,Q0ND",
        ),
        is_xtm=True,
    )

    assert payload["platform_logo"] == "/api/v1/platform/branding/asset/logo"
    assert payload["platform_favicon"] == "/api/v1/platform/branding/asset/favicon"
    assert payload["platform_og_image"] == "/api/v1/platform/branding/asset/og-image"
    for key in ("platform_logo_hash", "platform_favicon_hash", "platform_og_image_hash"):
        assert payload[key] and isinstance(payload[key], str)
    assert "base64" not in str(payload.values())


def test_resolved_payload_hash_changes_with_content():
    payload_a = resolved_branding_payload(
        Branding(name="Acme", logo="data:image/png;base64,QUFB"),
        is_xtm=True,
    )
    payload_b = resolved_branding_payload(
        Branding(name="Acme", logo="data:image/png;base64,QkJC"),
        is_xtm=True,
    )
    assert payload_a["platform_logo_hash"] != payload_b["platform_logo_hash"]


@pytest.mark.parametrize(
    ("field", "value", "expected_hash_key"),
    [
        ("logo", None, "platform_logo_hash"),
        ("logo", "https://cdn.example.com/logo.png", "platform_logo_hash"),
        ("favicon", None, "platform_favicon_hash"),
        ("og_image", None, "platform_og_image_hash"),
        ("og_image", "https://cdn.example.com/og.png", "platform_og_image_hash"),
    ],
    ids=[
        "logo_unset",
        "logo_legacy_url",
        "favicon_unset",
        "og_image_unset",
        "og_image_absolute_url",
    ],
)
def test_resolved_payload_no_hash_for_non_upload_values(field, value, expected_hash_key):
    payload = resolved_branding_payload(Branding(name="Acme", **{field: value}), is_xtm=True)
    assert payload[expected_hash_key] is None


def test_resolved_payload_favicon_none_when_unset():
    payload = resolved_branding_payload(Branding(name="Acme"), is_xtm=True)
    assert payload["platform_favicon"] is None
    assert payload["platform_favicon_hash"] is None


@pytest.mark.parametrize(
    ("is_xtm", "dark", "light"),
    [(True, "/xtp-one.svg", "/xtp-one-light.svg"), (False, "/filigran-copilot.svg", "/filigran-copilot-light.svg")],
    ids=["xtm_one", "copilot"],
)
def test_resolved_payload_serves_the_bundled_logos_light_variant(is_xtm, dark, light):
    # The bundled wordmarks are white: a client in the light theme needs the
    # dark-text variant, and the payload always carried the white one.
    payload = resolved_branding_payload(Branding(name="Acme"), is_xtm=is_xtm)
    assert (payload["platform_logo"], payload["platform_logo_light"]) == (dark, light)


@pytest.mark.parametrize(
    "logo",
    ["data:image/png;base64,QUFB", "https://cdn.example.com/logo.png"],
    ids=["upload", "legacy_url"],
)
def test_resolved_payload_has_no_light_variant_for_an_operators_logo(logo):
    payload = resolved_branding_payload(Branding(name="Acme", logo=logo), is_xtm=True)
    assert payload["platform_logo_light"] is None


@pytest.mark.parametrize(
    ("is_xtm", "dark", "light"),
    [(True, "/xtm-one-emblem.svg", "/xtm-one-emblem-light.svg"), (False, "/filigran-white.svg", "/filigran-black.svg")],
    ids=["xtm_one", "copilot"],
)
def test_resolved_payload_serves_the_bundled_emblem_for_the_collapsed_rail(is_xtm, dark, light):
    # A wordmark cannot fit the 28px collapsed rail: the client shows the mark
    # alone, in the variant of the theme it is drawn in.
    payload = resolved_branding_payload(Branding(name="Acme"), is_xtm=is_xtm)
    assert (payload["platform_emblem"], payload["platform_emblem_light"]) == (dark, light)
    assert payload["platform_emblem_hash"] is None


def test_resolved_payload_keeps_the_bundled_emblem_beside_the_bundled_logo():
    # An uploaded favicon alone does not rebrand the rail: the expanded rail
    # still shows the bundled wordmark, so the collapsed one shows its mark.
    payload = resolved_branding_payload(
        Branding(name="Acme", favicon="data:image/png;base64,QkJC"), is_xtm=True,
    )
    assert payload["platform_emblem"] == "/xtm-one-emblem.svg"


@pytest.mark.parametrize(
    "logo",
    ["data:image/png;base64,QUFB", "https://cdn.example.com/logo.png"],
    ids=["upload", "legacy_url"],
)
def test_resolved_payload_serves_the_uploaded_favicon_as_an_operators_emblem(logo):
    payload = resolved_branding_payload(
        Branding(name="Acme", logo=logo, favicon="data:image/png;base64,QkJC"), is_xtm=True,
    )
    assert payload["platform_emblem"] == "/api/v1/platform/branding/asset/favicon"
    assert payload["platform_emblem_hash"] == payload["platform_favicon_hash"]
    assert payload["platform_emblem_light"] is None


def test_resolved_payload_has_no_emblem_for_an_operators_logo_without_a_favicon():
    # Never another product's mark beside an operator's logo: with no square
    # mark of theirs, the client crops their logo to the rail instead.
    payload = resolved_branding_payload(
        Branding(name="Acme", logo="data:image/png;base64,QUFB"), is_xtm=False,
    )
    assert (payload["platform_emblem"], payload["platform_emblem_hash"], payload["platform_emblem_light"]) == (None, None, None)


def test_resolved_payload_logo_falls_back_to_legacy_url():
    payload = resolved_branding_payload(
        Branding(name="Acme", logo="https://cdn.example.com/logo.png"),
        is_xtm=True,
    )
    assert payload["platform_logo"] == "https://cdn.example.com/logo.png"


# ── mode_default_name per platform mode ──────────────────────────────────────

@pytest.mark.parametrize(
    ("platform_mode", "expected"),
    [
        ("xtm_one", "XTM One"),
        ("copilot", "Filigran Copilot"),
        ("dev", "Filigran Copilot"),
    ],
)
def test_mode_default_name(platform_mode, expected, monkeypatch):
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "platform_mode", platform_mode)
    assert mode_default_name() == expected


# ── env-var overrides: DB > env > mode default ───────────────────────────────


@pytest.fixture
def env_branding(monkeypatch):
    """Set the env-var branding overrides for a test. Returns a setter."""
    from app.core.config import settings as app_settings

    def _set(*, name: str = "", logo: str = ""):
        monkeypatch.setattr(app_settings, "platform_name", name)
        monkeypatch.setattr(app_settings, "platform_logo", logo)

    _set()  # start from a clean slate
    return _set


@pytest.mark.parametrize(
    ("db_name", "env_name", "expected"),
    [
        ("DB Brand", "Env Brand", "DB Brand"),
        (None, "Env Brand", "Env Brand"),
        ("", "Env Brand", "Env Brand"),
        (None, "   ", None),
        (None, "", None),
    ],
    ids=[
        "db_wins",
        "env_fills_null",
        "env_fills_blank",
        "whitespace_env_ignored",
        "mode_default",
    ],
)
def test_name_precedence(env_branding, db_name, env_name, expected):
    env_branding(name=env_name)
    resolved = _from_row(_PSStub(platform_name=db_name)).name
    assert resolved == (expected if expected is not None else mode_default_name())


@pytest.mark.parametrize(
    ("db_logo", "db_logo_url", "env_logo", "expected"),
    [
        (
            "data:image/png;base64,DB",
            None,
            "data:image/png;base64,ENV",
            "data:image/png;base64,DB",
        ),
        (None, "https://x/y.png", "data:image/png;base64,ENV", "https://x/y.png"),
        (None, None, "data:image/png;base64,ENV", "data:image/png;base64,ENV"),
        (None, None, "   ", None),
        (None, None, "", None),
    ],
    ids=[
        "db_base64_wins",
        "legacy_url_wins",
        "env_fills",
        "whitespace_env_ignored",
        "no_logo",
    ],
)
def test_logo_precedence(env_branding, db_logo, db_logo_url, env_logo, expected):
    env_branding(logo=env_logo)
    resolved = _from_row(
        _PSStub(platform_logo=db_logo, platform_logo_url=db_logo_url)
    ).logo
    assert resolved == expected

def test_from_row_falls_back_to_legacy_logo_url_when_stored_logo_is_svg():
    resolved = _from_row(
        _PSStub(
            platform_logo="data:image/svg+xml;base64,PHN2Zy8+",
            platform_logo_url="https://cdn.example.com/logo.png",
        )
    )

    assert resolved.logo == "https://cdn.example.com/logo.png"

def test_env_overrides_apply_when_no_db_row(env_branding):
    env_branding(name="Env Brand", logo="data:image/png;base64,ENV")
    b = _from_row(None)
    assert b.name == "Env Brand"
    assert b.logo == "data:image/png;base64,ENV"


@pytest.mark.asyncio
async def test_get_branding_does_not_mutate_sync_cache():
    branding_mod._cached_branding = Branding(name="Cached")
    loaded = await get_branding(_DBStub(_PSStub(platform_name="DB Brand")))

    assert loaded.name == "DB Brand"
    assert get_cached_branding().name == "Cached"


@pytest.mark.asyncio
async def test_refresh_branding_cache_updates_sync_cache():
    branding_mod._cached_branding = Branding(name="Cached")
    refreshed = await refresh_branding_cache(_DBStub(_PSStub(platform_name="DB Brand")))

    assert refreshed.name == "DB Brand"
    assert get_cached_branding().name == "DB Brand"


def test_build_web_manifest_uses_uploaded_assets_and_defaults():
    favicon = "data:image/png;base64,AAAA"
    logo = "data:image/png;base64,AAAA"
    content_hash = _data_url_hash(favicon)
    manifest = build_web_manifest(
        Branding(name="Acme", logo=logo, favicon=favicon),
        is_xtm=False,
    )

    assert manifest["name"] == "Acme"
    assert manifest["short_name"] == "Acme"
    assert manifest["description"] == "Next-gen enterprise agentic AI platform by Filigran"
    assert manifest["theme_color"] == "#0a0f1c"
    assert manifest["background_color"] == "#0a0f1c"
    assert manifest["icons"] == [
        {
            "src": f"/api/v1/platform/branding/asset/favicon?v={content_hash}",
            "sizes": "any",
            "type": "image/png",
        },
        {
            "src": f"/api/v1/platform/branding/asset/logo?v={content_hash}",
            "sizes": "any",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ]


def test_build_web_manifest_icon_src_changes_with_content():
    """Icon srcs must carry a content-derived ``?v=`` — the asset endpoint keeps
    ``Cache-Control: max-age=86400`` only for a URL whose ``v`` names the bytes
    it serves, so the versioned src is what lets browsers cache the PWA
    icons without keeping a replaced one."""
    manifest_a = build_web_manifest(
        Branding(name="Acme", favicon="data:image/png;base64,QUFB"), is_xtm=False
    )
    manifest_b = build_web_manifest(
        Branding(name="Acme", favicon="data:image/png;base64,QkJC"), is_xtm=False
    )
    assert manifest_a["icons"][0]["src"] != manifest_b["icons"][0]["src"]


@pytest.mark.parametrize(
    ("is_xtm", "expected_icon_srcs"),
    [
        (
            True,
            {
                "/xtp-one-icon.svg",
                "/xtm-one-icon-192.png",
                "/xtm-one-icon-512.png",
                "/xtm-one-icon-maskable-512.png",
            },
        ),
        (
            False,
            {
                "/favicon-16x16.png",
                "/favicon-32x32.png",
                "/apple-touch-icon.png",
                "/copilot-icon-192.png",
                "/copilot-icon-512.png",
                "/copilot-icon-maskable-512.png",
            },
        ),
    ],
    ids=["xtm", "copilot"],
)
def test_build_web_manifest_falls_back_to_mode_icons(is_xtm, expected_icon_srcs):
    manifest = build_web_manifest(Branding(name="Acme"), is_xtm=is_xtm)

    assert {icon["src"] for icon in manifest["icons"]} == expected_icon_srcs
    # Every default icon is a file the frontend ships.
    public = Path(__file__).resolve().parents[2] / "frontend" / "public"
    assert [src for src in expected_icon_srcs if not (public / src.lstrip("/")).is_file()] == []


@pytest.mark.parametrize("is_xtm", [True, False], ids=["xtm", "copilot"])
def test_the_default_manifest_is_installable_on_a_phone(is_xtm):
    """What a phone browser asks of a web app before it offers to install it:
    a stable id, a standalone display, and PNG icons of 192 and 512 px, one
    of them maskable."""
    manifest = build_web_manifest(Branding(name="Acme"), is_xtm=is_xtm, base_path="")
    assert (manifest["id"], manifest["start_url"], manifest["display"]) == ("/", "/", "standalone")
    pngs = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"] if icon["type"] == "image/png"}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= pngs
    assert [(s["name"], s["url"]) for s in manifest["shortcuts"]] == [
        ("Today", "/"),
        ("Inbox", "/inbox"),
        ("Ask", "/chat"),
    ]
    # Another app's "Share to" lands on the Share page through the service worker.
    assert (manifest["share_target"]["action"], manifest["share_target"]["method"]) == ("/share", "POST")


# ── _branding_head_version: manifest cache-buster reacts to content changes ──
#
# The version string feeds the ``?v=`` on the SSR ``<link rel="manifest">``.
# Every branding field that shapes the manifest (icons via logo/favicon,
# background_color, name, theme_color) must be part of the hash, otherwise
# browsers keep serving a cached manifest (and stale PWA icons) until
# max-age expiry.

@pytest.mark.parametrize(
    ("field", "value_a", "value_b"),
    [
        ("logo", "data:image/png;base64,QUFB", "data:image/png;base64,QkJC"),
        ("favicon", "data:image/png;base64,QUFB", "data:image/png;base64,QkJC"),
        ("og_image", "data:image/png;base64,QUFB", "data:image/png;base64,QkJC"),
        ("background_color", "#000000", "#111111"),
    ],
    ids=["logo", "favicon", "og_image", "background_color"],
)
def test_branding_head_version_changes_with_content(field, value_a, value_b):
    def version_for(value):
        branding = Branding(name="Acme", **{field: value})
        payload = resolved_branding_payload(branding, is_xtm=False)
        return _branding_head_version(branding, payload, is_xtm=False)

    assert version_for(value_a) != version_for(value_b)


def test_render_branded_index_html_versions_uploaded_og_image():
    og_image = "data:image/png;base64,QUFB"
    rendered = render_branded_index_html(
        '<html><head><meta property="og:image" content="/old.png"></head></html>',
        branding=Branding(name="Acme", og_image=og_image),
        is_xtm=False,
    )
    expected = (
        '<meta property="og:image" content='
        f'"/api/v1/platform/branding/asset/og-image?v={_data_url_hash(og_image)}">'
    )
    assert expected in rendered


def test_render_branded_index_html_patches_metadata_and_escapes_values():
    favicon = "data:image/png;base64,AAAA"
    html = (
        '<html><head><title>Old</title>'
        '<meta property="og:title" content="Old">'
        '<meta name="description" content="Old">'
        '<meta property="og:description" content="Old">'
        '<meta name="theme-color" content="#fff">'
        '<link rel="mask-icon" href="/mask.svg" color="#fff">'
        '<link rel="manifest" href="/site.webmanifest">'
        '<link rel="icon" href="/favicon.ico" sizes="any">'
        '<meta property="og:image" content="/old.png">'
        "</head></html>"
    )

    rendered = render_branded_index_html(
        html,
        branding=Branding(
            name='A & "B" \\1',
            description=r"Description & \g<1>",
            theme_color="#111111",
            favicon=favicon,
            og_image="/og.png",
        ),
        is_xtm=False,
    )

    expected_fragments = [
        r"<title>A &amp; &quot;B&quot; \1</title>",
        r'<meta property="og:title" content="A &amp; &quot;B&quot; \1">',
        r'<meta name="description" content="Description &amp; \g&lt;1&gt;">',
        r'<meta property="og:description" content="Description &amp; \g&lt;1&gt;">',
        '<meta name="theme-color" content="#111111">',
        '<link rel="icon" href="/api/v1/platform/branding/asset/favicon?v=',
        'sizes="any" />',
        '<link rel="apple-touch-icon" sizes="180x180" href="/api/v1/platform/branding/asset/favicon?v=',
        '<link rel="manifest" href="/api/v1/platform/manifest.webmanifest?v=',
        '<meta property="og:image" content="/og.png">',
    ]
    for fragment in expected_fragments:
        assert fragment in rendered
    assert favicon not in rendered
    assert 'rel="mask-icon"' not in rendered
    assert "/mask.svg" not in rendered


# ── Lifespan bootstrap: ensure platform_settings row + cache are coherent ──

class _BootstrapDBStub:
    """Fake AsyncSession that mimics SELECT-or-INSERT semantics.

    Initial ``execute()`` returns no row.  Once ``add()`` + ``flush()`` are
    called (by ``get_platform_settings``), subsequent ``execute()`` returns
    the captured row — modelling the round-trip from cold start through to
    ``refresh_branding_cache`` picking up the freshly inserted ``id``.
    """

    def __init__(self):
        self.row = None
        self.commits = 0

    async def execute(self, _stmt):
        return _ResultStub(self.row)

    def add(self, row):
        self.row = row

    async def flush(self):
        # The real ``PlatformSettings.id`` column has ``default=uuid.uuid4``
        # which fires at INSERT time; the stub mirrors that here so the
        # branding resolver sees a real UUID after the bootstrap call.
        import uuid

        if self.row is not None and getattr(self.row, "id", None) is None:
            self.row.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    def begin_nested(self):
        # ``get_platform_settings`` wraps its INSERT in a SAVEPOINT; the
        # stub returns a no-op async context manager standing in for
        # ``AsyncSessionTransaction``.
        return _SavepointStub()


class _SavepointStub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
async def test_lifespan_bootstrap_creates_row_and_primes_instance_id(env_branding):
    """Bootstrap sequence (helper → commit → refresh) yields a real instance_id.

    Without the helper call, ``refresh_branding_cache`` on an empty DB would
    fall back to ``_env_default_branding`` (``instance_id=None``), and every
    Langfuse trace stamped between cold start and the first admin write
    would carry ``instance_id="unknown"``.
    """
    env_branding()
    from app.api.v1.platform_settings_helpers import get_platform_settings

    branding_mod._cached_branding = Branding(name="Stale")

    db = _BootstrapDBStub()
    await get_platform_settings(db)
    await db.commit()
    await refresh_branding_cache(db)

    assert db.row is not None, "bootstrap should have inserted a singleton row"
    assert db.commits == 1, "the inserted row should be committed before refresh"
    cached = get_cached_branding()
    assert cached.instance_id is not None
    # UUID string form: 32 hex digits + 4 dashes.
    assert len(cached.instance_id) == 36

