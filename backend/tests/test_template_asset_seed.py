"""Brand asset seeding for the built-in template containers.

Two halves:

* ``svg_sanitizer`` — the bytes seeded into every deployment (and any SVG an
  admin uploads as a template asset) are cleaned ONCE on the way into storage.
  These tests pin what is stripped, what is kept verbatim, and that a document
  which cannot be parsed is refused rather than half-cleaned.
* ``template_asset_seed`` — idempotent by construction: a deterministic object
  key derived from the content hash means "already seeded" is decided from the
  rows alone, with no S3 round-trip; a changed file is refreshed in place (same
  row id, so the decks placing it keep it) and a retired one is removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.svg_sanitizer import (
    SvgSanitizeError,
    is_svg_content_type,
    sanitize_svg,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ── SVG sanitizer ─────────────────────────────────────────────────────────────


def _svg(body: str, attrs: str = "") -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10" {attrs}>'
        f"{body}</svg>"
    ).encode()


def test_clean_svg_keeps_its_geometry_and_paint():
    src = _svg('<path d="M0 0h10v10H0z" fill="#42caff" opacity="0.5"/>')
    out = sanitize_svg(src).decode()
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in out
    assert 'd="M0 0h10v10H0z"' in out
    assert 'fill="#42caff"' in out
    assert "ns0:" not in out, "the SVG namespace must stay the default one"


@pytest.mark.parametrize(
    "body",
    [
        "<script>alert(1)</script>",
        '<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">x</body></foreignObject>',
        '<iframe src="https://evil.example"/>',
        '<animate attributeName="href" to="javascript:alert(1)"/>',
        '<set attributeName="href" to="javascript:alert(1)"/>',
        '<use href="https://evil.example/x.svg#a"/>',
        '<use xlink:href="https://evil.example/x.svg#a"/>',
        '<image href="https://evil.example/pixel.png"/>',
    ],
)
def test_executable_or_outbound_elements_are_dropped(body):
    out = sanitize_svg(_svg(body + '<rect width="1" height="1"/>')).decode()
    assert "evil.example" not in out
    assert "alert" not in out
    for tag in ("script", "foreignObject", "iframe", "animate", "<set", "<use", "<image"):
        assert tag not in out
    assert "<rect" in out, "siblings of a dropped element survive"


def test_local_use_and_embedded_raster_are_kept():
    body = '<defs><path id="p" d="M0 0h1"/></defs><use href="#p"/><image href="data:image/png;base64,iVBORw0KGgo="/>'
    out = sanitize_svg(_svg(body)).decode()
    assert 'href="#p"' in out
    assert "data:image/png;base64" in out


@pytest.mark.parametrize(
    ("attrs", "gone", "kept"),
    [
        ('onload="alert(1)" fill="red"', "onload", 'fill="red"'),
        ('onclick="x()" id="a"', "onclick", 'id="a"'),
        (
            'style="fill:red;background:url(https://evil.example/x)" id="b"',
            "style",
            'id="b"',
        ),
        ('style="fill:red" id="c"', "nothing", 'style="fill:red"'),
    ],
)
def test_handlers_and_outbound_styles_are_removed_from_kept_elements(attrs, gone, kept):
    out = sanitize_svg(_svg(f"<rect {attrs}/>")).decode()
    if gone != "nothing":
        assert gone not in out
    assert kept in out
    assert "evil.example" not in out


def test_javascript_href_on_a_link_is_dropped_but_web_links_stay():
    body = (
        '<a href="javascript:alert(1)"><text>x</text></a>'
        '<a href="https://filigran.io"><text>y</text></a>'
        '<a href="mailto:hello@filigran.io"><text>z</text></a>'
        '<a href="#local"><text>w</text></a>'
    )
    out = sanitize_svg(_svg(body)).decode()
    assert "javascript:" not in out
    assert 'href="https://filigran.io"' in out
    assert 'href="mailto:hello@filigran.io"' in out
    assert 'href="#local"' in out


def test_a_stray_href_on_a_shape_may_only_reference_the_document():
    body = (
        '<textPath href="https://evil.example/p"><tspan>a</tspan></textPath>'
        '<textPath href="#p"><tspan>b</tspan></textPath>'
    )
    out = sanitize_svg(_svg(body)).decode()
    assert "evil.example" not in out
    assert 'href="#p"' in out
    assert out.count("<textPath") == 2, "the element stays, only the href goes"


def test_style_blocks_that_reach_out_are_dropped_and_local_ones_kept():
    body = '<style>@import url(https://evil.example/x.css);</style><style>.a{fill:#fff}</style><rect class="a"/>'
    out = sanitize_svg(_svg(body)).decode()
    assert "evil.example" not in out
    assert "@import" not in out
    assert ".a{fill:#fff}" in out


def test_doctype_and_processing_instructions_are_not_re_emitted():
    """The DOCTYPE Illustrator / Inkscape emit is harmless and accepted; it and
    any processing instruction are simply not part of the output."""
    src = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
        b'<?xml-stylesheet href="https://evil.example/s.css"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
    )
    out = sanitize_svg(src).decode()
    assert "DOCTYPE" not in out
    assert "xml-stylesheet" not in out
    assert "evil.example" not in out
    assert out.startswith("<svg")


@pytest.mark.parametrize(
    "src",
    [
        # Inline entity: the billion-laughs / XXE primitive. Never legitimate in a logo.
        b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        # External entity: reads a local file into the document.
        b'<!DOCTYPE svg [<!ENTITY f SYSTEM "file:///etc/passwd">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&f;</text></svg>',
    ],
)
def test_entity_declarations_are_refused_not_expanded(src):
    with pytest.raises(SvgSanitizeError, match="forbidden XML construct"):
        sanitize_svg(src)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg><rect></svg>",
        b"not xml at all",
        b"",
        b'<html xmlns="http://www.w3.org/1999/xhtml"><svg/></html>',
    ],
)
def test_unparseable_or_non_svg_input_is_refused_not_half_cleaned(data):
    with pytest.raises(SvgSanitizeError):
        sanitize_svg(data)
    # A ValueError subclass, so the seed's generic guard catches it.
    assert issubclass(SvgSanitizeError, ValueError)


def test_a_root_spelled_svg_in_a_foreign_namespace_is_refused():
    """An XHTML-namespaced ``svg`` root is an XHTML document to the browser:
    its ``meta refresh`` redirects and its ``img`` beacons whatever the local
    name says. Refused outright, like a non-svg root."""
    xhtml_root = (
        b'<svg xmlns="http://www.w3.org/1999/xhtml">'
        b'<meta http-equiv="refresh" content="0;url=https://evil.example"/>'
        b'<img src="https://evil.example/t.gif"/></svg>'
    )
    with pytest.raises(SvgSanitizeError, match="root element is not <svg>"):
        sanitize_svg(xhtml_root)
    # A bare root with no namespace stays accepted (inert markup, and the
    # shape the package fixtures use).
    assert sanitize_svg(b"<svg><rect/></svg>") == b"<svg><rect /></svg>"


def test_elements_in_a_foreign_namespace_are_dropped_inside_a_real_svg():
    """XHTML elements keep their semantics inside an XML document wherever
    they sit (``foreignObject`` or not); Inkscape / RDF metadata renders
    nothing. Both go; the SVG's own elements stay."""
    body = (
        '<html:meta xmlns:html="http://www.w3.org/1999/xhtml" http-equiv="refresh" content="0;url=https://evil.example"/>'
        '<html:img xmlns:html="http://www.w3.org/1999/xhtml" src="https://evil.example/t.gif"/>'
        '<sodipodi:namedview xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" id="base"/>'
        '<rect width="10" height="10" fill="#fff"/>'
    )
    out = sanitize_svg(_svg(body)).decode()
    assert "evil.example" not in out
    assert "refresh" not in out and "namedview" not in out
    assert '<rect width="10" height="10" fill="#fff" />' in out


def test_css_escapes_cannot_spell_a_reaching_keyword():
    r"""``\75rl(`` is ``url(`` once the browser decodes the CSS escape; the
    style check reads the decoded text. A harmless escape elsewhere stays."""
    body = (
        '<style>.a{background:\\75rl(https://evil.example/x.png)}</style>'
        '<style>.b{content:"\\201C"}</style>'
        '<rect class="a" style="fill:\\75rl(#p)"/>'
        '<rect class="b" style="fill:#fff"/>'
    )
    out = sanitize_svg(_svg(body)).decode()
    assert "evil.example" not in out
    assert "\\75rl" not in out
    assert 'content:"\\201C"' in out, "an escape that spells nothing reaching stays"
    assert 'style="fill:#fff"' in out


def test_sanitizing_is_idempotent():
    src = _svg('<script>x</script><rect onload="y" fill="#fff"/>')
    once = sanitize_svg(src)
    assert sanitize_svg(once) == once


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("image/svg+xml", True),
        ("image/svg+xml; charset=utf-8", True),
        ("IMAGE/SVG+XML", True),
        ("image/png", False),
        (None, False),
        ("", False),
    ],
)
def test_is_svg_content_type(value, expected):
    assert is_svg_content_type(value) is expected


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("image/svg+xml", "logo.bin", True),
        ("application/octet-stream", "logo.svg", True),
        ("image/png", "LOGO.SVG", True),
        ("image/png", "logo.png", False),
        (None, None, False),
    ],
)
def test_looks_like_svg_takes_either_signal(content_type, filename, expected):
    """A mislabelled upload must not be able to skip the clean."""
    from app.services.svg_sanitizer import looks_like_svg

    assert looks_like_svg(content_type, filename) is expected


def test_sanitize_template_asset_cleans_svgs_and_passes_everything_else_through():
    from app.services.svg_sanitizer import sanitize_template_asset

    png = b"\x89PNG\r\n\x1a\n<script>not xml, not touched</script>"
    assert sanitize_template_asset(png, "image/png", "x.png") is png
    dirty = _svg("<script>x</script><rect/>")
    assert b"script" not in sanitize_template_asset(dirty, "image/svg+xml", "logo.svg")
    # Extension alone triggers the clean; a .svg that is not SVG is refused.
    assert b"script" not in sanitize_template_asset(dirty, "application/octet-stream", "a.svg")
    with pytest.raises(SvgSanitizeError):
        sanitize_template_asset(b"garbage", "image/png", "a.svg")


# ── One write path for every template asset ───────────────────────────────────


def test_store_template_asset_bytes_sanitizes_before_upload(monkeypatch):
    from app.services.file_storage import file_storage
    from app.services.template_containers import (
        store_template_asset_bytes,
        template_asset_prefix,
    )

    uploads: list[dict] = []

    async def _upload(*, data, filename, content_type, prefix):
        uploads.append({"data": data, "filename": filename, "ct": content_type, "prefix": prefix})
        return f"{prefix}/{uuid.uuid4()}-{filename}"

    monkeypatch.setattr(file_storage, "upload", _upload)
    cid = uuid.uuid4()
    stored, key = asyncio.run(
        store_template_asset_bytes(
            cid, _svg("<script>x</script><rect/>"), filename="l.svg", content_type="image/svg+xml"
        )
    )
    assert b"script" not in stored
    assert uploads[0]["data"] == stored, "what is uploaded is the CLEAN document"
    assert uploads[0]["prefix"] == template_asset_prefix(cid) == f"template-asset/{cid}"
    assert key.startswith(f"template-asset/{cid}/")

    with pytest.raises(SvgSanitizeError):
        asyncio.run(store_template_asset_bytes(cid, b"<svg><x>", filename="b.svg", content_type="image/svg+xml"))
    assert len(uploads) == 1, "a refused SVG never reaches the bucket"


def test_every_ingestion_point_goes_through_the_one_write_path():
    """REST upload / replace, package import + duplicate, and the agent's
    attach copy must not upload template bytes on their own — that is the only
    way the sanitizer is guaranteed to have run on what the deck renders."""
    for rel in (
        "app/api/v1/template_containers.py",
        "app/services/builtin_tools/template_authoring.py",
    ):
        source = (BACKEND_ROOT / rel).read_text(encoding="utf-8")
        assert "file_storage.upload(" not in source, f"{rel} bypasses store_template_asset_bytes"
        assert "store_template_asset_bytes" in source
    api = (BACKEND_ROOT / "app/api/v1/template_containers.py").read_text(encoding="utf-8")
    assert api.count("await _store_asset(") == 3, "add, replace and import all store through it"


# ── The shipped brand assets ──────────────────────────────────────────────────


def _brand_dir() -> Path:
    from app.services.template_asset_seed import BRAND_ASSETS_DIR

    return BRAND_ASSETS_DIR


def test_every_shipped_brand_asset_is_already_clean():
    """A built-in asset ships into every deployment: it must not NEED cleaning.

    If this fails, the sanitizer would silently alter what a designer exported;
    fix the file at the source instead of relying on the strip.
    """
    for path in sorted(_brand_dir().glob("*/*.svg")):
        data = path.read_bytes()
        cleaned = sanitize_svg(data)
        lowered = data.lower()
        for needle in (b"<script", b"onload", b"onclick", b"foreignobject", b"javascript:"):
            assert needle not in lowered, f"{path.name} carries {needle!r}"
        # Re-serialization may normalise whitespace / attribute order, so compare
        # structure through a second pass rather than the raw bytes.
        assert sanitize_svg(cleaned) == cleaned, path.name


#: What the first bytes of a seeded asset must look like, per content type. A
#: mislabelled file (a PNG saved as ``.webp``) would seed fine and then render
#: as a broken image on every deck — the manifest loader trusts the extension.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/svg+xml": (b"<svg",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


#: Libraries the Copilot templates include that this repository does not ship:
#: logos and badges without release permission (#11458).
PRIVATE_LIBRARIES = (
    "filigran-badges",
    "filigran-partner-logos",
    "filigran-integration-logos",
    "filigran-customer-logos",
)


def _merged_shipped_assets():
    from app.services.template_asset_seed import load_brand_manifests, merge_manifests

    return merge_manifests(load_brand_manifests())


def test_shipped_manifests_bind_declared_containers_and_known_roles():
    from app.models.template_container import ASSET_ROLES, MAX_ASSETS_PER_CONTAINER
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    declared = {c["slug"] for c in BUILTIN_TEMPLATE_CONTAINERS}
    merged = _merged_shipped_assets()
    assert set(merged) == declared, (
        "every built-in container is fed by a manifest, and every manifest names one"
    )
    for slug, assets in merged.items():
        assert assets, f"{slug} has no assets"
        assert len(assets) <= MAX_ASSETS_PER_CONTAINER, f"{slug} ships more assets than a container may hold"
        filenames = [a.filename for a in assets]
        assert len(filenames) == len(set(filenames)), f"{slug}: duplicate filename across its packs"
        for asset in assets:
            assert asset.role in ASSET_ROLES
            assert asset.content_type in _MAGIC, (
                f"{asset.filename}: {asset.content_type} is not a format the deck renders"
            )
            assert asset.data.startswith(_MAGIC[asset.content_type]), (
                f"{asset.filename} does not start like a {asset.content_type}"
            )
            if asset.content_type == "image/webp":
                assert asset.data[8:12] == b"WEBP", f"{asset.filename} is RIFF but not WebP"
            assert asset.description, f"{asset.filename} has no description for the agent"


def test_no_shipped_manifest_entry_is_silently_skipped():
    """The loader SKIPS (with a warning) an entry that is oversized, missing or
    an invalid SVG — right for a deployment, wrong for the build: a predefined
    asset that never seeds is exactly the regression the seed exists to prevent."""
    from app.services.template_asset_seed import load_brand_manifests

    manifests = load_brand_manifests()
    paths = sorted(_brand_dir().glob("*/manifest.json"))
    assert len(manifests) == len(paths), "one BrandManifest per pack, in folder order"
    for manifest, manifest_path in zip(manifests, paths, strict=True):
        raw = json.loads(manifest_path.read_text("utf-8"))
        assert manifest.container_slug == raw.get("container", ""), "no container: a library"
        assert len(manifest.assets) == len(raw["assets"]), (
            f"{manifest_path.parent.name}: some manifest entries did not load"
        )
        assert manifest.skipped == 0
    from app.services.template_asset_seed import incomplete_containers

    gaps = incomplete_containers(manifests)
    assert all(set(packs) <= set(PRIVATE_LIBRARIES) for packs in gaps.values()), (
        f"an include names a pack that is neither shipped nor a private library: {gaps}"
    )


def test_the_private_libraries_never_ship_and_feed_copilot_templates_only():
    """The packs without release permission stay out of the repository for
    good: no folder of theirs under ``brand_assets/``. Only templates that
    ship on Copilot alone include them, and each of them is included."""
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    folders = {path.name for path in _brand_dir().iterdir() if path.is_dir()}
    assert not (folders & set(PRIVATE_LIBRARIES)), "a private library is back in the public repository"
    copilot_only = {c["slug"] for c in BUILTIN_TEMPLATE_CONTAINERS if c["platform_targets"] == ["copilot"]}
    included: set[str] = set()
    for manifest_path in sorted(_brand_dir().glob("*/manifest.json")):
        raw = json.loads(manifest_path.read_text("utf-8"))
        private = {i["pack"] for i in raw.get("include", [])} & set(PRIVATE_LIBRARIES)
        if private:
            assert raw.get("container") in copilot_only, (manifest_path.parent.name, private)
        included |= private
    assert included == set(PRIVATE_LIBRARIES), "a private library no template includes is a stale name"


def test_the_brand_deck_ships_the_product_and_platform_diagrams():
    """The predefined figures an agent is told to place instead of drawing:
    the XTM platform architecture and each product's flow. Their
    captions must name the product so ``get_template`` lets the agent pick by
    description, and they must be rasters at deck resolution or clean SVGs."""
    brand = _merged_shipped_assets()["filigran-brand"]
    diagrams = [a for a in brand if a.role == "diagram"]
    assert len(diagrams) >= 6, "the brand deck lost its diagram set"
    captions = " ".join(a.description.lower() for a in diagrams)
    for product in ("xtm platform", "opencti", "openaev", "xtm one", "ctem"):
        assert product in captions, f"no diagram caption mentions {product!r}"
    roles = {a.role for a in brand}
    for role in ("screenshot", "illustration", "product_logo", "image"):
        assert role in roles, f"the brand deck ships no {role!r} asset"


def test_the_logo_packs_are_predefined_assets_of_the_brand_deck():
    """There is no ``asset-library`` container any more. The partner,
    integration and customer logos ship as predefined assets of the Filigran
    Brand Deck, each under its own role so ``get_template(role=...)`` separates
    them, and the retired library slugs name the brand deck as their heir so an
    existing install moves its rows instead of losing them.

    The logo packs and the badge pack are private libraries the Filigran pack
    includes: a deployment serves them from its brand asset source, and one
    that does not holds the brand deck's retirements instead of dropping its
    logos."""
    from app.services.template_container_seed import (
        BUILTIN_TEMPLATE_CONTAINERS,
        RETIRED_ASSET_HOMES,
    )
    from app.services.template_containers import ASSET_LIBRARY_KIND, TEMPLATE_KINDS

    assert all(c["kind"] in TEMPLATE_KINDS for c in BUILTIN_TEMPLATE_CONTAINERS)
    assert not any(c["kind"] == ASSET_LIBRARY_KIND for c in BUILTIN_TEMPLATE_CONTAINERS)
    assert RETIRED_ASSET_HOMES == {
        "filigran-partner-logos": "filigran-brand",
        "filigran-integration-logos": "filigran-brand",
        "filigran-customer-logos": "filigran-brand",
    }
    declared = {c["slug"] for c in BUILTIN_TEMPLATE_CONTAINERS}
    assert set(RETIRED_ASSET_HOMES.values()) <= declared, "an heir must be a declared built-in"
    assert not (set(RETIRED_ASSET_HOMES) & declared), "a retired slug is not declared any more"

    filigran = json.loads((_brand_dir() / "filigran" / "manifest.json").read_text("utf-8"))
    assert filigran["container"] == "filigran-brand"
    assert [i["pack"] for i in filigran["include"]] == list(PRIVATE_LIBRARIES)
    assert not any(e["role"] == "badge" for e in filigran["assets"]), "the badges live in their own pack"


def test_every_pack_that_took_the_whole_filigran_pack_takes_its_libraries():
    """The badges were part of the Filigran pack, and the logo libraries fed
    the brand deck: a template that includes that pack whole (its chrome roles
    aside) also includes every private library, so moving them out took no
    badge or logo away from it. The templates of every platform take the
    Filigran product families by name, never the badges."""
    for manifest_path in sorted(_brand_dir().glob("*/manifest.json")):
        raw = json.loads(manifest_path.read_text("utf-8"))
        includes = raw.get("include", [])
        filigran = next((i for i in includes if i["pack"] == "filigran"), None)
        if filigran is None or "only_roles" in filigran:
            continue
        assert "badge" not in filigran.get("except_roles", [])
        assert set(PRIVATE_LIBRARIES) <= {i["pack"] for i in includes}, manifest_path.parent.name


def test_merge_manifests_folds_packs_per_container_and_keeps_one_of_a_duplicate(caplog):
    from app.services.template_asset_seed import merge_manifests

    a, b, c = _asset("a.svg"), _asset("b.svg", "partner_logo"), _asset("c.svg", "customer_logo")
    b_again = _asset("b.svg", "customer_logo", '<circle r="2"/>')
    merged = merge_manifests(
        [
            _manifest(a, slug="filigran-brand"),
            _manifest(b, slug="filigran-brand"),
            _manifest(c, slug="xtm-one-brand"),
            _manifest(b_again, slug="filigran-brand"),
        ]
    )
    assert [x.filename for x in merged["filigran-brand"]] == ["a.svg", "b.svg"]
    assert merged["filigran-brand"][1] is b, "the first pack wins"
    assert [x.filename for x in merged["xtm-one-brand"]] == ["c.svg"]
    assert "b.svg twice" in caplog.text


def test_seeded_key_tail_reads_the_container_independent_half():
    from app.services.template_asset_seed import seed_object_name, seeded_key_tail

    key = seed_object_name(uuid.uuid4(), "c" * 64, "logo.svg")
    assert seeded_key_tail(key) == f"{'c' * 16}-logo.svg"
    assert seeded_key_tail(f"template-asset/{uuid.uuid4()}/admin-upload.svg") is None, "admin upload"
    assert seeded_key_tail("conversation/abc/seed/x.svg") is None
    assert seeded_key_tail(None) is None
    assert seeded_key_tail(f"template-asset/{uuid.uuid4()}/seed/") is None


def test_seeded_digest_prefix_is_the_head_of_the_digest_a_seeded_key_carries():
    """What a seeded key says about its bytes: the first
    ``SEED_DIGEST_PREFIX_CHARS`` hex characters of their SHA-256 - a prefix to
    compare with ``startswith``, never the digest. An uploaded asset's
    key carries none."""
    from app.services.template_asset_seed import (
        SEED_DIGEST_PREFIX_CHARS,
        seed_object_name,
        seed_tail,
        seeded_digest_prefix,
    )

    digest = "0123456789abcdef" * 4
    key = seed_object_name(uuid.uuid4(), digest, "brand-logo-dark.svg")
    assert seeded_digest_prefix(key) == digest[:SEED_DIGEST_PREFIX_CHARS] == "0123456789abcdef"
    assert seed_tail(digest, "logo.svg") == f"{digest[:SEED_DIGEST_PREFIX_CHARS]}-logo.svg", "one length, two readers"
    assert seeded_digest_prefix(f"template-asset/{uuid.uuid4()}/{uuid.uuid4().hex}.svg") is None, "an upload"
    assert seeded_digest_prefix(f"template-asset/{uuid.uuid4()}/seed/not-a-digest.svg") is None
    assert seeded_digest_prefix(f"template-asset/{uuid.uuid4()}/seed/0123456789abcdef.svg") is None, "no dash"
    assert seeded_digest_prefix(None) is None


def test_every_manifest_declares_every_file_in_its_folder():
    """A logo dropped in the folder but not listed is never seeded — and the
    reverse is caught by ``load_brand_manifests`` at boot with a warning only.
    An entry borrowing a sibling pack's file (``"pack"``) names a file of THAT
    folder, which must exist there."""
    for manifest_path in sorted(_brand_dir().glob("*/manifest.json")):
        entries = json.loads(manifest_path.read_text("utf-8"))["assets"]
        listed = {e["file"] for e in entries if "pack" not in e}
        on_disk = {p.name for p in manifest_path.parent.iterdir() if p.name != "manifest.json"}
        assert listed == on_disk, f"{manifest_path.parent.name}: {listed ^ on_disk}"
        for entry in entries:
            if "pack" in entry:
                assert (_brand_dir() / entry["pack"] / entry["file"]).is_file(), entry


def test_the_chrome_logo_roles_are_present_on_every_brand():
    """``resolve_template_theme`` derives ``logo_file_id`` from the ``logo``
    asset — a brand without one renders a deck with no logo at all."""
    roles = {slug: {a.role for a in assets} for slug, assets in _merged_shipped_assets().items()}
    assert "logo" in roles["filigran-brand"]
    assert "logo_light" in roles["filigran-brand"], "the light theme needs the dark-on-light mark"
    assert "product_logo" in roles["filigran-brand"]
    assert "logo" in roles["xtm-one-brand"]


def test_the_two_filigran_wordmarks_are_distinct_variants():
    root = _brand_dir() / "filigran"
    dark = (root / "filigran-logo.svg").read_bytes()
    light = (root / "filigran-logo-light.svg").read_bytes()
    assert dark != light, "logo_light must be the dark-on-light variant, not a copy"


# ── Manifest loading: packaging mistakes are skipped, never fatal ─────────────


def _write_manifest(folder: Path, assets: list[dict], container: str = "filigran-brand"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"container": container, "assets": assets}), encoding="utf-8")


def test_manifest_loader_skips_bad_entries_and_keeps_the_good_one(tmp_path, caplog):
    from app.services import template_asset_seed as mod

    brand = tmp_path / "brand"
    (brand / "ok.svg").parent.mkdir(parents=True)
    (brand / "ok.svg").write_bytes(_svg('<rect onload="x"/>'))
    (brand / "bad.svg").write_bytes(b"<svg><unclosed></svg>")
    (brand / "big.svg").write_bytes(b"x" * (mod.MAX_SEED_ASSET_BYTES + 1))
    _write_manifest(
        brand,
        [
            {"file": "ok.svg", "role": "logo", "description": "fine"},
            {"file": "bad.svg", "role": "logo"},
            {"file": "big.svg", "role": "logo"},
            {"file": "missing.svg", "role": "logo"},
            {"file": "../escape.svg", "role": "logo"},
            {"file": "ok.svg", "role": "not-a-role"},
            "not-a-dict",
        ],
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "shapeless").mkdir()
    (tmp_path / "shapeless" / "manifest.json").write_text('{"container": "filigran-brand"}', encoding="utf-8")

    manifests = mod.load_brand_manifests(tmp_path)
    assert [m.container_slug for m in manifests] == ["filigran-brand"]
    (asset,) = manifests[0].assets
    assert asset.filename == "ok.svg"
    assert asset.role == "logo"
    assert asset.content_type == "image/svg+xml"
    assert b"onload" not in asset.data, "sanitized before hashing"
    assert asset.description == "fine"


def _write_pack(root: Path, pack: str, container: str, files: dict[str, str], assets: list[dict], **extra):
    folder = root / pack
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (folder / name).write_bytes(_svg(body))
    (folder / "manifest.json").write_text(
        json.dumps({"container": container, "assets": assets, **extra}), encoding="utf-8"
    )


def _write_library(root: Path, pack: str, files: dict[str, str], assets: list[dict]):
    """A pack with no ``container`` key: fed to the containers that include it."""
    _write_pack(root, pack, "", files, assets)
    manifest = root / pack / "manifest.json"
    raw = json.loads(manifest.read_text("utf-8"))
    del raw["container"]
    manifest.write_text(json.dumps(raw), encoding="utf-8")


def test_a_pack_includes_a_sibling_pack_minus_roles_and_borrows_one_file(tmp_path, caplog):
    """A container takes a sibling pack's OWN entries (never what that pack
    includes in turn) minus the roles it excludes, and an entry borrows one
    file from a sibling pack under the role and caption it gives it."""
    from app.services import template_asset_seed as mod

    _write_pack(
        tmp_path,
        "base",
        "a-brand",
        {"base-logo.svg": "<rect/>", "diagram.svg": "<circle r='1'/>"},
        [
            {"file": "base-logo.svg", "role": "logo", "description": "base logo"},
            {"file": "diagram.svg", "role": "diagram", "description": "the diagram"},
        ],
    )
    _write_pack(
        tmp_path,
        "libs",
        "a-brand",
        {"partner.svg": "<rect width='2'/>"},
        [{"file": "partner.svg", "role": "partner_logo", "description": "a partner"}],
        include=[{"pack": "base"}],
    )
    _write_pack(
        tmp_path,
        "derived",
        "b-brand",
        {"own-logo.svg": "<rect height='3'/>"},
        [
            {"file": "own-logo.svg", "role": "logo", "description": "the derived logo"},
            {"file": "base-logo.svg", "pack": "base", "role": "product_logo", "description": "borrowed"},
        ],
        include=[
            {"pack": "base", "except_roles": ["logo"]},
            {"pack": "libs"},
            {"pack": "missing"},
            {"pack": "../escape"},
            {"pack": "derived"},
        ],
    )
    manifests = mod.load_brand_manifests(tmp_path)
    assert [m.pack for m in manifests] == ["base", "derived", "libs"]
    derived = next(m for m in manifests if m.pack == "derived")
    assert [i.pack for i in derived.includes] == ["base", "libs", "missing"], "the malformed targets are skipped"
    merged = mod.merge_manifests(manifests)
    roles = {asset.filename: (asset.role, asset.description) for asset in merged["b-brand"]}
    assert roles == {
        "own-logo.svg": ("logo", "the derived logo"),
        "base-logo.svg": ("product_logo", "borrowed"),
        "diagram.svg": ("diagram", "the diagram"),
        "partner.svg": ("partner_logo", "a partner"),
    }, "base's logo is excluded, libs' own include of base is not followed"
    assert [asset.role for asset in merged["b-brand"]].count("logo") == 1
    assert mod.incomplete_containers(manifests) == {"b-brand": ("missing",)}
    # The packs that feed a-brand are untouched by derived's includes.
    assert sorted(asset.filename for asset in merged["a-brand"]) == ["base-logo.svg", "diagram.svg", "partner.svg"]


def test_a_borrowed_entry_naming_an_invalid_pack_is_skipped(tmp_path, caplog):
    from app.services import template_asset_seed as mod

    _write_pack(
        tmp_path,
        "solo",
        "a-brand",
        {"own.svg": "<rect/>"},
        [
            {"file": "own.svg", "role": "logo", "description": "own"},
            {"file": "x.svg", "pack": "..", "role": "icon", "description": "escape"},
            {"file": "own.svg", "pack": "solo", "role": "icon", "description": "itself"},
        ],
    )
    (manifest,) = mod.load_brand_manifests(tmp_path)
    assert [asset.filename for asset in manifest.assets] == ["own.svg"]
    assert "names an invalid pack" in caplog.text


def test_an_include_names_the_roles_it_takes_and_its_own_entries_win(tmp_path, caplog):
    """``only_roles`` takes the named families alone, minus any
    ``except_roles``; a role either list names must exist (a misspelt exclusion
    would let its family through) and an include that admits nothing is
    refused; a file the manifest lists itself is not taken again from the
    include - its own entry is kept, with no duplicate report."""
    from app.services import template_asset_seed as mod

    _write_pack(
        tmp_path,
        "base",
        "a-brand",
        {"logo.svg": "<rect/>", "diagram.svg": "<circle r='1'/>", "team.svg": "<rect width='4'/>"},
        [
            {"file": "logo.svg", "role": "product_logo", "description": "the base's copy of the logo"},
            {"file": "diagram.svg", "role": "diagram", "description": "the diagram"},
            {"file": "team.svg", "role": "people_photo", "description": "a portrait"},
        ],
    )
    _write_pack(
        tmp_path,
        "product",
        "b-brand",
        {"logo.svg": "<rect height='5'/>"},
        [{"file": "logo.svg", "role": "logo", "description": "the product's own logo"}],
        include=[
            {"pack": "base", "only_roles": ["product_logo", "diagram"]},
            {"pack": "base", "except_roles": ["people_photos"]},
            {"pack": "base", "only_roles": ["diagrams"]},
            {"pack": "base", "only_roles": []},
            {"pack": "base", "only_roles": ["diagram"], "except_roles": ["diagram"]},
        ],
    )
    _write_pack(
        tmp_path,
        "combo",
        "c-brand",
        {},
        [],
        include=[{"pack": "base", "only_roles": ["product_logo", "people_photo"], "except_roles": ["people_photo"]}],
    )
    manifests = mod.load_brand_manifests(tmp_path)
    product = next(m for m in manifests if m.pack == "product")
    (include,) = product.includes
    assert include.only_roles == frozenset({"product_logo", "diagram"})
    assert caplog.text.count("invalid include") == 4, "a misspelt, empty or cancelled role list refuses the include"
    caplog.clear()
    merged = mod.merge_manifests(manifests)
    assert [(a.filename, a.role, a.description) for a in merged["b-brand"]] == [
        ("logo.svg", "logo", "the product's own logo"),
        ("diagram.svg", "diagram", "the diagram"),
    ]
    assert b"height" in merged["b-brand"][0].data, "the manifest's own file, not the included one"
    assert [(a.filename, a.role) for a in merged["c-brand"]] == [("logo.svg", "product_logo")], (
        "only_roles names the families, except_roles removes from them"
    )
    assert "twice" not in caplog.text


#: The families of the Filigran pack a template of every platform takes:
#: public product material, never Filigran's customers, partners or people.
_PRODUCT_ROLES = frozenset({"product_logo", "icon", "diagram", "screenshot"})


@pytest.mark.usefixtures("video_studio_enabled")
def test_the_templates_of_every_platform_ship_the_product_material_and_nothing_copilot_only():
    """The Filigran library stays on Filigran Copilot; the XTM One templates
    get their product families. A template that ships outside
    Copilot holds its own marks and those families alone: no customer, partner
    or integration logo, no team photo, badge, illustration or Filigran
    wordmark - by role and by file, so a file borrowed under another role is
    caught too."""
    from app.services.template_container_seed import builtin_containers_for

    merged = _merged_shipped_assets()
    copilot_only = {a.filename for a in merged["filigran-brand"] if a.role not in _PRODUCT_ROLES}
    # The reference set is the Filigran deck's: it must hold the photo and
    # illustration families, or this test would pass against a set that misses
    # what it guards. The logo and badge libraries are private
    # (PRIVATE_LIBRARIES): no template of every platform includes them.
    guarded = {a.role for a in merged["filigran-brand"] if a.filename in copilot_only}
    assert {"people_photo", "image", "illustration", "logo"} <= guarded
    shipped = builtin_containers_for("xtm_one")
    everywhere = [c["slug"] for c in shipped]
    # A report template draws its logos alone: no slide or scene places a figure.
    reports = {c["slug"] for c in shipped if c["kind"] == "report"}
    assert "xtm-one-brand" in everywhere and "xtm-one-video" in everywhere
    assert reports == {"xtm-one-report"}
    for slug in everywhere:
        roles = {a.role for a in merged[slug]}
        assert roles <= _PRODUCT_ROLES | {"logo", "logo_light", "logo_mark"}, (slug, roles - _PRODUCT_ROLES)
        if slug not in reports:
            assert {"product_logo", "diagram", "screenshot"} <= roles, f"{slug} lost the product material"
        leaked = {a.filename for a in merged[slug]} & copilot_only
        assert not leaked, (slug, leaked)


def test_every_template_holds_at_most_one_asset_per_chrome_role():
    """The deck chrome, the video cards and the watermark draw the `logo`,
    `logo_light` and `logo_mark` asset by role: a second one - a pack included
    without its chrome roles excluded or its files overridden - would make the
    drawn mark arbitrary. A manifest's ``except_roles`` list is JSON and cannot
    share a constant, so this is what catches one that drifts."""
    from app.services.video_studio.spec import BRAND_ASSETS

    for slug, assets in _merged_shipped_assets().items():
        for role in BRAND_ASSETS:
            holders = [a.filename for a in assets if a.role == role]
            assert len(holders) <= 1, (slug, role, holders)


def test_every_deck_naming_the_product_figures_ships_them():
    """The product brief the decks share names the XTM platform, OpenCTI,
    OpenAEV, XTM One agents and CTEM figures: a deck whose guidance
    carries it ships a diagram captioned for each, and product logos and
    screenshots, or the brief sends the agent to a figure that is not there."""
    from app.services.template_container_seed import _PRODUCT_ASSETS_GUIDANCE, BUILTIN_TEMPLATE_CONTAINERS

    merged = _merged_shipped_assets()
    decks = [
        c["slug"]
        for c in BUILTIN_TEMPLATE_CONTAINERS
        if _PRODUCT_ASSETS_GUIDANCE in (c["extra_metadata"].get("guidance") or "")
    ]
    assert {"filigran-brand", "thread-brand", "xtm-one-brand"} <= set(decks)
    for slug in decks:
        captions = " ".join(a.description.lower() for a in merged[slug] if a.role == "diagram")
        for figure in ("xtm platform", "opencti", "openaev", "xtm one", "ctem"):
            assert figure in captions, (slug, figure)
        assert {"product_logo", "screenshot"} <= {a.role for a in merged[slug]}, slug


def test_the_thread_deck_ships_the_thread_identity_and_every_filigran_figure():
    """The THREAD deck's chrome draws ITS logo and emblem, never the Filigran
    ones (the Filigran pack's chrome roles are excluded, its wordmark borrowed
    as a product logo), and it carries the Filigran figures and logo libraries
    a keynote shows. Its light scheme draws the same lockup in dark
    ink: the same paths, only the white fill changed."""
    thread = _merged_shipped_assets()["thread-brand"]
    by_role: dict[str, list[str]] = {}
    for asset in thread:
        by_role.setdefault(asset.role, []).append(asset.filename)
    assert by_role["logo"] == ["thread-logo.svg"]
    assert by_role["logo_light"] == ["thread-logo-dark.svg"]
    assert by_role["logo_mark"] == ["thread-emblem.png"]
    files = {asset.filename: asset.data for asset in thread}
    assert files["thread-logo-dark.svg"] == files["thread-logo.svg"].replace(b'fill="white"', b'fill="#0a0a0a"')
    assert b'fill="white"' not in files["thread-logo-dark.svg"]
    assert {"filigran-logo.svg", "filigran-mark.svg"} <= set(by_role["product_logo"])
    assert set(by_role["illustration"]) >= {"thread-mosaic-cluster.webp", "thread-mosaic-scatter.webp"}
    brand = _merged_shipped_assets()["filigran-brand"]
    for role in ("diagram", "screenshot"):
        expected = {a.filename for a in brand if a.role == role}
        assert expected and expected <= set(by_role.get(role, [])), role


def test_manifest_loader_tolerates_a_missing_root(tmp_path, caplog):
    from app.services.template_asset_seed import load_brand_manifests

    assert load_brand_manifests(tmp_path / "nope") == []
    assert "is not a folder" in caplog.text


def test_a_library_feeds_only_the_containers_that_include_it(tmp_path, caplog):
    """A pack with no ``container`` is a library: no container of its own,
    its entries reach the packs that include it. A ``container`` key that
    is present but empty is a mistake, not a library."""
    from app.services import template_asset_seed as mod

    _write_pack(
        tmp_path,
        "brand",
        "a-brand",
        {"logo.svg": "<rect/>"},
        [{"file": "logo.svg", "role": "logo"}],
        include=[{"pack": "logos"}],
    )
    _write_library(tmp_path, "logos", {"p.svg": "<circle r='1'/>"}, [{"file": "p.svg", "role": "partner_logo"}])
    _write_pack(tmp_path, "empty", "", {}, [])

    manifests = mod.load_brand_manifests(tmp_path)
    assert [(m.pack, m.container_slug) for m in manifests] == [("brand", "a-brand"), ("logos", "")]
    assert "Skipping malformed brand manifest" in caplog.text, "an empty container is refused"
    merged = mod.merge_manifests(manifests)
    assert list(merged) == ["a-brand"]
    assert [a.filename for a in merged["a-brand"]] == ["logo.svg", "p.svg"]


def test_extra_roots_add_packs_and_a_pack_name_loads_once(tmp_path, monkeypatch, caplog):
    """``BRAND_ASSETS_EXTRA_DIRS`` names roots read after the bundled one: an
    include reaches a pack of any root; a pack folder name a root already
    loaded is skipped in a later one; a root that is not there is reported."""
    from app.core.config import settings
    from app.services import template_asset_seed as mod

    bundled, private = tmp_path / "bundled", tmp_path / "private"
    _write_pack(
        bundled,
        "brand",
        "a-brand",
        {"logo.svg": "<rect/>"},
        [{"file": "logo.svg", "role": "logo"}],
        include=[{"pack": "logos"}],
    )
    _write_library(private, "logos", {"p.svg": "<circle r='1'/>"}, [{"file": "p.svg", "role": "partner_logo"}])
    _write_pack(
        private, "brand", "b-brand", {"other.svg": "<rect width='2'/>"}, [{"file": "other.svg", "role": "logo"}]
    )
    monkeypatch.setattr(mod, "BRAND_ASSETS_DIR", bundled)
    monkeypatch.setattr(settings, "brand_assets_extra_dirs", f" {private} ,, {tmp_path / 'absent'}")

    assert mod.brand_asset_roots() == (bundled, private, tmp_path / "absent")
    manifests = mod.load_brand_manifests()
    assert [(m.pack, m.container_slug) for m in manifests] == [("brand", "a-brand"), ("logos", "")]
    assert "Brand pack brand of" in caplog.text and "already loaded" in caplog.text
    assert "absent is not a folder" in caplog.text
    assert [a.filename for a in mod.merge_manifests(manifests)["a-brand"]] == ["logo.svg", "p.svg"]
    assert mod.incomplete_containers(manifests) == {}


def test_a_container_fed_by_a_pack_that_did_not_load_whole_is_incomplete(tmp_path):
    """A pack an include names that no root holds, and a pack that loaded
    with an entry skipped, both leave the container short of files still
    meant to ship."""
    from app.services import template_asset_seed as mod

    _write_pack(
        tmp_path,
        "a",
        "a-brand",
        {"x.svg": "<rect/>"},
        [{"file": "x.svg", "role": "logo"}],
        include=[{"pack": "gone"}, {"pack": "lib"}],
    )
    _write_pack(
        tmp_path,
        "b",
        "b-brand",
        {"y.svg": "<rect/>"},
        [{"file": "y.svg", "role": "logo"}, {"file": "missing.svg", "role": "icon"}],
    )
    _write_pack(
        tmp_path, "c", "c-brand", {"z.svg": "<rect/>"}, [{"file": "z.svg", "role": "logo"}], include=[{"pack": "lib"}]
    )
    _write_library(tmp_path, "lib", {}, [{"file": "absent.svg", "role": "partner_logo"}])

    manifests = mod.load_brand_manifests(tmp_path)
    assert {m.pack: m.skipped for m in manifests} == {"a": 0, "b": 1, "c": 0, "lib": 1}
    assert mod.incomplete_containers(manifests) == {
        "a-brand": ("gone", "lib"),
        "b-brand": ("b",),
        "c-brand": ("lib",),
    }


def test_seed_object_name_is_deterministic_and_marked_as_seeded():
    from app.services.template_asset_seed import SEED_KEY_SEGMENT, seed_object_name

    cid = uuid.uuid4()
    a = seed_object_name(cid, "a" * 64, "logo.svg")
    assert a == seed_object_name(cid, "a" * 64, "logo.svg")
    assert a.startswith(f"template-asset/{cid}/{SEED_KEY_SEGMENT}/")
    assert a.endswith("-logo.svg")
    assert seed_object_name(cid, "b" * 64, "logo.svg") != a, "content change = new key"
    assert seed_object_name(uuid.uuid4(), "a" * 64, "logo.svg") != a


# ── Idempotent sync against fakes ─────────────────────────────────────────────


class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return _Scalars(self._rows)


class _Session:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.added: list = []
        self.deleted: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, *a, **k):
        return self._results.pop(0) if self._results else _Result()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Storage:
    def __init__(self, fail_upload: bool = False, fail_delete: bool = False):
        self.uploads: list[tuple[str, str, int]] = []
        self.deletes: list[str] = []
        self._fail_upload = fail_upload
        self._fail_delete = fail_delete

    async def upload_to(self, data: bytes, key: str, content_type: str):
        if self._fail_upload:
            raise ConnectionError("minio down")
        self.uploads.append((key, content_type, len(data)))

    async def delete(self, key: str):
        if self._fail_delete:
            raise ConnectionError("minio down")
        self.deletes.append(key)


def _asset(
    filename: str = "logo.svg",
    role: str = "logo",
    body: str = "<rect/>",
    description: str | None = None,
):
    from app.services.template_asset_seed import SeedAsset

    return SeedAsset(
        filename=filename,
        role=role,
        content_type="image/svg+xml",
        data=_svg(body),
        description=description,
    )


def _manifest(*assets, slug: str = "filigran-brand"):
    from app.services.template_asset_seed import BrandManifest

    return BrandManifest(container_slug=slug, assets=tuple(assets))


def _container():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Filigran Brand Deck")


def _row_for(container, asset, role=None, description=None):
    from app.services.template_asset_seed import seed_object_name

    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        asset_role=role or asset.role,
        description=description,
        filename=asset.filename,
        s3_object_name=seed_object_name(container.id, asset.digest, asset.filename),
    )


def _sync(session, container, manifest, storage, obsolete=None, *, retire=True):
    from app.services.template_asset_seed import _sync_container_assets

    return asyncio.run(
        _sync_container_assets(
            session,
            container,
            manifest.assets,
            storage,
            obsolete_objects=obsolete if obsolete is not None else [],
            retire=retire,
        )
    )


def test_first_boot_uploads_every_asset_under_its_content_key():
    from app.models.template_container import TEMPLATE_ASSET_SOURCE
    from app.services.template_asset_seed import seed_object_name

    container = _container()
    logo, mark = _asset(), _asset("mark.svg", "logo_mark", '<circle r="1"/>')
    session, storage = _Session([_Result([])]), _Storage()

    assert _sync(session, container, _manifest(logo, mark), storage) == 2
    assert [k for k, _, _ in storage.uploads] == [
        seed_object_name(container.id, logo.digest, "logo.svg"),
        seed_object_name(container.id, mark.digest, "mark.svg"),
    ]
    assert len(session.added) == 2
    row = session.added[0]
    assert row.template_container_id == container.id
    assert row.user_id == container.user_id
    assert row.source == TEMPLATE_ASSET_SOURCE
    assert row.asset_role == "logo"
    assert row.content_type == "image/svg+xml"
    assert row.size == len(logo.data)
    assert session.deleted == []


def test_first_boot_writes_the_manifest_description_normalized():
    """The caption is what an agent picks the asset by: it lands on the
    row, one line, whatever the manifest's whitespace."""
    container = _container()
    diagram = _asset(
        "xtm-platform.svg",
        "illustration",
        description="  XTM platform:\n  four products around\tone hub.  ",
    )
    session, storage = _Session([_Result([])]), _Storage()

    _sync(session, container, _manifest(diagram), storage)
    (row,) = session.added
    assert row.description == "XTM platform: four products around one hub."


def test_second_boot_with_unchanged_content_touches_the_store_not_at_all():
    container = _container()
    logo = _asset()
    session = _Session([_Result([_row_for(container, logo)])])
    storage = _Storage()

    assert _sync(session, container, _manifest(logo), storage) == 0
    assert storage.uploads == [] and storage.deletes == []
    assert session.added == [] and session.deleted == []


def test_changed_content_is_refreshed_in_place_and_keeps_the_row_id():
    """A deck places a template asset by its row id. A refreshed brand
    file (same name, new bytes) is the SAME asset: the row keeps its id and takes
    the new object, so every deck showing it shows the new version instead of a
    hole - the promise ``get_template`` makes and the admin replace route keeps.
    The old object is handed back, to be deleted once the new key is committed."""
    container = _container()
    old = _asset(body='<rect fill="#000"/>', description="Old caption.")
    new = _asset(body='<rect fill="#fff"/>', description="New caption.")
    row = _row_for(container, old)
    row_id, old_key = row.id, row.s3_object_name
    session, storage, obsolete = _Session([_Result([row])]), _Storage(), []

    assert _sync(session, container, _manifest(new), storage, obsolete) == 1
    (uploaded_key, content_type, size) = storage.uploads[0]
    assert uploaded_key != old_key and uploaded_key.endswith("-logo.svg")
    assert row.id == row_id, "the id every deck placed is kept"
    assert row.s3_object_name == uploaded_key
    assert (row.size, row.content_type) == (size, content_type) == (len(new.data), "image/svg+xml")
    assert row.description == "New caption."
    assert session.added == [] and session.deleted == []
    assert storage.deletes == [], "the old object goes after the commit, never before"
    assert obsolete == [old_key]


def test_a_refreshed_asset_reads_the_geometry_of_its_new_bytes():
    """A deck crops a logo to the trim box stored on its row. A refresh
    in place keeps the row, so it must replace the box too - the old file's
    box would crop the new one, and nothing else re-reads a row that has one."""
    container = _container()
    old = _asset(body='<rect width="10" height="2" fill="#000"/>')
    new = _asset(body='<rect x="2" y="2" width="6" height="6" fill="#000"/>')
    row = _row_for(container, old)
    row.image_meta = {"v": 1, "w": 512, "h": 512, "trim": [0.0, 0.0, 1.0, 0.2], "ink": 1.0}
    session, storage = _Session([_Result([row])]), _Storage()

    assert _sync(session, container, _manifest(new), storage) == 1
    left, top, right, bottom = row.image_meta["trim"]
    assert abs(left - 0.2) <= 0.01 and abs(top - 0.2) <= 0.01
    assert abs(right - 0.8) <= 0.01 and abs(bottom - 0.8) <= 0.01


def test_a_refreshed_asset_records_the_digest_of_its_new_bytes():
    """The refresh points the row at a new object, so the digest it
    records moves with it - a stale one would let a presentation package import
    take this row for the file it USED to hold, without reading it."""
    container = _container()
    old = _asset(body='<rect fill="#000"/>')
    new = _asset(body='<rect fill="#fff"/>')
    row = _row_for(container, old)
    row.sha256 = old.digest
    session, storage = _Session([_Result([row])]), _Storage()

    assert _sync(session, container, _manifest(new), storage) == 1
    assert row.sha256 == new.digest


def test_a_seeded_asset_records_the_digest_of_the_bytes_it_uploads():
    container = _container()
    logo = _asset()
    session, storage = _Session([_Result([])]), _Storage()

    _sync(session, container, _manifest(logo), storage)
    (row,) = session.added
    assert row.sha256 == logo.digest == hashlib.sha256(logo.data).hexdigest()


def test_a_renamed_file_is_a_new_asset_and_the_old_name_is_retired():
    """Only the same filename is the same asset: a file shipped under a new name
    is uploaded as a new row, and the row of the name no longer shipped goes -
    its object handed back for after the commit, like a refreshed one's."""
    container = _container()
    old = _asset("logo-2024.svg", body="<rect/>")
    new = _asset("logo-2026.svg", body="<circle/>")
    old_row = _row_for(container, old)
    old_key = old_row.s3_object_name
    session, storage, obsolete = _Session([_Result([old_row])]), _Storage(), []

    assert _sync(session, container, _manifest(new), storage, obsolete) == 1
    assert [row.filename for row in session.added] == ["logo-2026.svg"]
    assert session.deleted == [old_row]
    assert storage.deletes == []
    assert obsolete == [old_key]


def test_a_removed_manifest_entry_removes_its_seeded_row():
    """An asset dropped from the manifest disappears from the seeded template
    on the next sync - the seeded rows alone: an asset a user attached to the
    container (no ``seed`` segment in its key) is never part of the comparison
    and survives, whatever the manifests ship."""
    container = _container()
    kept, retired = _asset(), _asset("old.svg", "icon")
    retired_row = _row_for(container, retired)
    user_row = _row_for(container, _asset("ours.svg", "image"))
    user_row.s3_object_name = f"template-asset/{container.id}/{uuid.uuid4()}-ours.svg"
    session = _Session([_Result([_row_for(container, kept), retired_row, user_row])])
    storage, obsolete = _Storage(), []

    assert _sync(session, container, _manifest(kept), storage, obsolete) == 0
    assert session.deleted == [retired_row], "the seeded row goes, the user's stays"
    assert storage.uploads == [] and storage.deletes == [], "the object goes after the commit, never before"
    assert obsolete == [retired_row.s3_object_name]


def test_a_held_sync_retires_nothing_but_still_takes_new_and_changed_files():
    """A container a pack did not load whole for keeps every seeded row the
    manifests no longer list - they are missing, not retired - while a new
    file is still seeded and a changed one still refreshed in place."""
    container = _container()
    absent_row = _row_for(container, _asset("partner.svg", "partner_logo"))
    changed_row = _row_for(container, _asset("logo.svg", body="<rect/>"))
    previous_key = changed_row.s3_object_name
    session = _Session([_Result([absent_row, changed_row])])
    storage, obsolete = _Storage(), []

    shipped = _manifest(_asset("logo.svg", body="<circle/>"), _asset("new.svg", "icon"))
    assert _sync(session, container, shipped, storage, obsolete, retire=False) == 2
    assert session.deleted == [], "the missing file's row stays"
    assert [row.filename for row in session.added] == ["new.svg"]
    assert changed_row.s3_object_name in {key for key, _, _ in storage.uploads}, "refreshed in place"
    assert obsolete == [previous_key], "only the refreshed file's previous object goes"


def test_labels_are_code_managed_on_unchanged_content():
    """A role renamed or a description reworded in the manifest lands without a
    re-upload; an ownerless row inherits the container's owner."""
    container = _container()
    asset = _asset(role="logo_mark", description="Filigran symbol, no wordmark.")
    row = _row_for(container, asset, role="logo", description="Filigran symbol.")
    session, storage = _Session([_Result([row])]), _Storage()

    _sync(session, container, _manifest(asset), storage)
    assert row.asset_role == "logo_mark"
    assert row.description == "Filigran symbol, no wordmark."
    assert row.user_id == container.user_id
    assert storage.uploads == []


def test_the_sync_itself_never_deletes_an_object():
    """Objects are deleted by the boot entry point after the commit, never by
    the sync: an object store that refuses deletes cannot fail the sync."""
    container = _container()
    retired_row = _row_for(container, _asset("old.svg", body="<rect/>"))
    session = _Session([_Result([retired_row])])
    storage, obsolete = _Storage(fail_delete=True), []

    assert _sync(session, container, _manifest(_asset(body="<circle/>")), storage, obsolete) == 1
    assert session.deleted == [retired_row]
    assert obsolete == [retired_row.s3_object_name]


def test_a_row_re_homed_from_a_retired_container_is_recognised_by_its_seed_tail():
    """The logo packs moved into the brand deck. Their rows keep the
    retired container's segment in the key; the sync must see the same asset
    (same content hash + filename) and neither re-upload nor delete it - the
    row id is what a deck's ``logo_grid`` references."""
    container = _container()
    retired = _container()
    logo = _asset("partner.svg", "partner_logo")
    moved_row = _row_for(retired, logo, role="partner_logo")
    moved_row.template_container_id = container.id
    assert f"/{retired.id}/" in moved_row.s3_object_name
    session, storage = _Session([_Result([moved_row])]), _Storage()

    assert _sync(session, container, _manifest(logo), storage) == 0
    assert storage.uploads == [] and storage.deletes == []
    assert session.added == [] and session.deleted == []
    assert moved_row.user_id == container.user_id, "an ownerless moved row takes the heir's owner"


def test_a_re_homed_row_with_stale_content_is_refreshed_like_any_other():
    container, retired = _container(), _container()
    old = _asset("partner.svg", "partner_logo", "<rect/>")
    new = _asset("partner.svg", "partner_logo", "<circle/>")
    moved_row = _row_for(retired, old)
    old_key = moved_row.s3_object_name
    session, storage, obsolete = _Session([_Result([moved_row])]), _Storage(), []

    assert _sync(session, container, _manifest(new), storage, obsolete) == 1
    assert session.deleted == [] and session.added == []
    (uploaded_key, _, _) = storage.uploads[0]
    assert f"/{container.id}/" in uploaded_key, "a new upload lands under the heir's own prefix"
    assert moved_row.s3_object_name == uploaded_key
    assert obsolete == [old_key]


# ── The boot entry point ──────────────────────────────────────────────────────


def _run_seed(monkeypatch, *, manifests, containers_by_slug, storage, mode="copilot"):
    from app.services import template_asset_seed as mod
    from app.services.file_storage import file_storage

    monkeypatch.setattr(mod, "load_brand_manifests", lambda *roots: list(manifests))

    async def _find(_db, slug):
        return containers_by_slug.get(slug)

    monkeypatch.setattr(mod, "_load_builtin_container", _find)
    monkeypatch.setattr(file_storage, "upload_to", storage.upload_to, raising=False)
    monkeypatch.setattr(file_storage, "delete", storage.delete, raising=False)
    # One empty asset-row result per container the seed will look at.
    session = _Session([_Result([]) for _ in containers_by_slug])
    uploaded = asyncio.run(mod.seed_builtin_template_assets(session, platform_mode=mode))
    return uploaded, session


@pytest.mark.parametrize(
    ("mode", "shipped"),
    [
        ("copilot", {"filigran-brand", "xtm-one-brand"}),
        ("xtm_one", {"xtm-one-brand"}),
        ("dev", {"xtm-one-brand"}),
    ],
)
def test_boot_seeds_only_the_containers_the_mode_ships(monkeypatch, mode, shipped):
    """The Filigran packs - customer and partner logos, team photos - reach a
    Copilot platform only; the XTM One pack ships everywhere."""
    containers = {"filigran-brand": _container(), "xtm-one-brand": _container()}
    storage = _Storage()
    uploaded, session = _run_seed(
        monkeypatch,
        manifests=[
            _manifest(_asset(), slug="filigran-brand"),
            _manifest(_asset("x.svg"), slug="xtm-one-brand"),
        ],
        containers_by_slug=containers,
        storage=storage,
        mode=mode,
    )
    assert uploaded == len(shipped)
    fed = {slug for slug, row in containers.items() if any(f"/{row.id}/" in k for k, _, _ in storage.uploads)}
    assert fed == shipped
    assert session.commits == len(shipped)


def test_boot_merges_the_packs_that_feed_one_container_into_one_sync(monkeypatch):
    """Three packs feed the brand deck. Synced one pack at a time, each
    pass would delete the other packs' rows as stale; merged, one pass sees the
    whole expected set and commits once."""
    filigran = _container()
    storage = _Storage()
    uploaded, session = _run_seed(
        monkeypatch,
        manifests=[
            _manifest(_asset("logo.svg"), slug="filigran-brand"),
            _manifest(_asset("partner.svg", "partner_logo"), slug="filigran-brand"),
            _manifest(_asset("customer.svg", "customer_logo"), slug="filigran-brand"),
        ],
        containers_by_slug={"filigran-brand": filigran},
        storage=storage,
    )
    assert uploaded == 3
    assert session.deleted == []
    assert session.commits == 1


def test_boot_holds_the_retirements_of_a_container_whose_library_did_not_load(monkeypatch, caplog):
    """The brand deck includes a logo library no root holds (the private root
    not mounted): its seeded logos are kept, not deleted as stale, and the
    boot says which pack is missing. A container fed whole retires as usual."""
    from app.services import template_asset_seed as mod
    from app.services.file_storage import file_storage
    from app.services.template_asset_seed import BrandManifest, PackInclude

    brand, other = _container(), _container()
    logo, partner = _asset("logo.svg"), _asset("partner.svg", "partner_logo")
    manifests = [
        BrandManifest("filigran-brand", (logo,), pack="filigran", includes=(PackInclude("filigran-partner-logos"),)),
        BrandManifest("xtm-one-brand", (_asset("x.svg"),), pack="xtm-one"),
    ]
    monkeypatch.setattr(mod, "load_brand_manifests", lambda *roots: list(manifests))
    containers = {"filigran-brand": brand, "xtm-one-brand": other}

    async def _find(_db, slug):
        return containers.get(slug)

    monkeypatch.setattr(mod, "_load_builtin_container", _find)
    storage = _Storage()
    monkeypatch.setattr(file_storage, "upload_to", storage.upload_to, raising=False)
    monkeypatch.setattr(file_storage, "delete", storage.delete, raising=False)
    kept = _row_for(brand, partner)
    retired = _row_for(other, _asset("old.svg"))
    session = _Session([_Result([_row_for(brand, logo), kept]), _Result([retired])])

    asyncio.run(mod.seed_builtin_template_assets(session, platform_mode="copilot"))
    assert session.deleted == [retired], "the brand deck keeps its logos; the other container retires"
    assert storage.deletes == [retired.s3_object_name]
    assert "filigran-partner-logos did not load whole" in caplog.text


def test_a_seed_told_not_to_retire_holds_every_container(monkeypatch):
    """A configured brand asset source that was not read may hold a pack that
    feeds a container directly (its own ``container``), which no include
    reveals: ``retire=False`` keeps the seeded rows of every container, still
    taking their new files."""
    from app.services import template_asset_seed as mod
    from app.services.file_storage import file_storage
    from app.services.template_asset_seed import BrandManifest

    brand = _container()
    logo, fresh = _asset("logo.svg"), _asset("fresh.svg")
    monkeypatch.setattr(
        mod, "load_brand_manifests", lambda *roots: [BrandManifest("xtm-one-brand", (logo, fresh), pack="xtm-one")]
    )

    async def _find(_db, slug):
        return brand if slug == "xtm-one-brand" else None

    monkeypatch.setattr(mod, "_load_builtin_container", _find)
    storage = _Storage()
    monkeypatch.setattr(file_storage, "upload_to", storage.upload_to, raising=False)
    monkeypatch.setattr(file_storage, "delete", storage.delete, raising=False)
    from_source = _row_for(brand, _asset("from-source.svg"))
    session = _Session([_Result([_row_for(brand, logo), from_source])])

    uploaded = asyncio.run(mod.seed_builtin_template_assets(session, platform_mode="xtm_one", retire=False))
    assert session.deleted == [] and storage.deletes == [], "the source's file is missing, not removed"
    assert uploaded == 1, "the new file is still taken"


def test_the_loader_names_the_manifests_and_roots_it_could_not_read(tmp_path):
    """A manifest that does not parse, or names no list of assets, and a root
    that is not a folder: the packs they hold are unknown, so they are named."""
    from app.services import template_asset_seed as mod

    _write_pack(tmp_path, "good", "a-brand", {"a.svg": "<rect/>"}, [{"file": "a.svg", "role": "logo"}])
    (tmp_path / "typo").mkdir()
    (tmp_path / "typo" / "manifest.json").write_text('{"container": "a-brand",', "utf-8")
    (tmp_path / "shapeless").mkdir()
    (tmp_path / "shapeless" / "manifest.json").write_text('{"container": "a-brand"}', "utf-8")

    manifests = mod.load_brand_manifests(tmp_path, tmp_path / "absent")
    assert [m.pack for m in manifests] == ["good"]
    assert manifests.unreadable == ("shapeless", "typo", str(tmp_path / "absent"))


def test_an_unreadable_manifest_holds_every_container(monkeypatch, caplog):
    """A pack feeding a template directly whose manifest got a typo: its rows
    must not read as removed, so no container retires anything that seed."""
    from app.services import template_asset_seed as mod
    from app.services.file_storage import file_storage
    from app.services.template_asset_seed import BrandManifest, LoadedManifests

    brand = _container()
    logo = _asset("logo.svg")
    manifests = LoadedManifests([BrandManifest("xtm-one-brand", (logo,), pack="xtm-one")])
    manifests.unreadable = ("acme-deck",)
    monkeypatch.setattr(mod, "load_brand_manifests", lambda *roots: manifests)

    async def _find(_db, slug):
        return brand if slug == "xtm-one-brand" else None

    monkeypatch.setattr(mod, "_load_builtin_container", _find)
    storage = _Storage()
    monkeypatch.setattr(file_storage, "upload_to", storage.upload_to, raising=False)
    monkeypatch.setattr(file_storage, "delete", storage.delete, raising=False)
    session = _Session([_Result([_row_for(brand, logo), _row_for(brand, _asset("from-acme.svg"))])])

    asyncio.run(mod.seed_builtin_template_assets(session, platform_mode="xtm_one"))
    assert session.deleted == [] and storage.deletes == []
    assert "acme-deck could not be read" in caplog.text


def test_boot_skips_a_manifest_whose_container_is_missing(monkeypatch, caplog):
    storage = _Storage()
    uploaded, session = _run_seed(
        monkeypatch,
        manifests=[_manifest(_asset(), slug="filigran-brand")],
        containers_by_slug={"filigran-brand": None},
        storage=storage,
    )
    assert uploaded == 0
    assert storage.uploads == []
    assert session.commits == 0


def test_boot_survives_an_object_store_outage(monkeypatch, caplog):
    """The metadata sync already committed; a dead MinIO must not take boot
    down — the deck renders without a logo until the next startup retries."""
    storage = _Storage(fail_upload=True)
    uploaded, session = _run_seed(
        monkeypatch,
        manifests=[_manifest(_asset(), slug="filigran-brand")],
        containers_by_slug={"filigran-brand": _container()},
        storage=storage,
    )
    assert uploaded == 0
    assert session.rollbacks == 1
    assert session.commits == 0
    assert "will retry on next startup" in caplog.text


def _run_ordered_seed(monkeypatch, row, shipped, *, fail_commit=False, fail_delete=False):
    """Boot the asset seed over one stored *row* and the *shipped* manifest,
    recording the order of commits and object deletes."""
    from app.services import template_asset_seed as mod
    from app.services.file_storage import file_storage

    container = _container()
    events: list[str] = []

    class _OrderedSession(_Session):
        async def commit(self):
            events.append("commit")
            if fail_commit:
                raise ConnectionError("database went away")
            await super().commit()

    class _OrderedStorage(_Storage):
        async def delete(self, key):
            events.append(f"delete {key}")
            await super().delete(key)

    async def _find(_db, slug):
        return container if slug == "filigran-brand" else None

    storage = _OrderedStorage(fail_delete=fail_delete)
    monkeypatch.setattr(mod, "load_brand_manifests", lambda *roots: [_manifest(*shipped)])
    monkeypatch.setattr(mod, "_load_builtin_container", _find)
    monkeypatch.setattr(file_storage, "upload_to", storage.upload_to, raising=False)
    monkeypatch.setattr(file_storage, "delete", storage.delete, raising=False)
    session = _OrderedSession([_Result([row])])
    uploaded = asyncio.run(mod.seed_builtin_template_assets(session, platform_mode="copilot"))
    return uploaded, session, events


@pytest.mark.parametrize("delete_fails", [False, True])
@pytest.mark.parametrize("change", ["refreshed", "retired"])
def test_boot_deletes_an_obsolete_object_only_after_the_commit(monkeypatch, caplog, delete_fails, change):
    """A refreshed row must point at its new object durably before the old one
    goes, like the admin replace route, and a retired row must be gone
    durably before its object is: a failure after the commit leaks one object,
    never the asset every deck places."""
    row = _row_for(_container(), _asset(body="<rect/>"))
    old_key = row.s3_object_name
    shipped = [_asset(body="<circle/>")] if change == "refreshed" else [_asset("other.svg", body="<circle/>")]

    uploaded, session, events = _run_ordered_seed(monkeypatch, row, shipped, fail_delete=delete_fails)
    assert uploaded == 1
    assert events == ["commit", f"delete {old_key}"]
    assert session.rollbacks == 0
    if change == "refreshed":
        assert row.s3_object_name != old_key and session.deleted == []
    else:
        assert session.deleted == [row]
    if delete_fails:
        assert f"Could not delete the obsolete seeded asset object {old_key}" in caplog.text


@pytest.mark.parametrize("change", ["refreshed", "retired"])
def test_a_rolled_back_sync_deletes_no_object(monkeypatch, caplog, change):
    """The failure the ordering exists for: a commit that fails rolls every row
    back to the object it named, so that object must still exist - deleting a
    retired asset's object before the commit left a restored row naming
    nothing, and every deck placing it with a hole."""
    row = _row_for(_container(), _asset(body="<rect/>"))
    shipped = [_asset(body="<circle/>")] if change == "refreshed" else [_asset("other.svg", body="<circle/>")]

    _uploaded, session, events = _run_ordered_seed(monkeypatch, row, shipped, fail_commit=True)
    assert session.rollbacks == 1
    assert events == ["commit"], "no object is deleted when the rows were not committed"
    assert "will retry on next startup" in caplog.text


def test_provisioning_seeds_assets_right_after_the_containers():
    """Order matters: the asset seed looks the container rows up by slug."""
    import inspect

    from app.services import provisioning

    source = inspect.getsource(provisioning)
    assert source.index("seed_builtin_template_containers(") < source.index("seed_template_assets(")
