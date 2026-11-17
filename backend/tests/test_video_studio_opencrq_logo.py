"""The Brand Film's OpenCRQ logo is a vector one, like the other products'.

The Filigran brand pack drew OpenCTI, OpenAEV, XTM One and XTM Hub as SVG
logos (mark + wordmark) but OpenCRQ only as a 992x208 PNG: an image scene of it
was drawn twice its size on a 1080p frame and four times on a 2160p one, and
the density check failed it. What is proven:

* ``opencrq-logo.svg`` and ``opencrq-logo-light.svg`` ship with the role
  ``product_logo`` in the brand pack and in the Brand Film, a vector mark and
  wordmark in the colours of the other products' logos;
* no description sends an agent to the PNG for the logo any more: the PNGs
  stay, for the videos that already place them, saying to place the SVG;
* an image scene of the SVG passes the density check at 1080p and 2160p,
  where the PNG fails it.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from types import SimpleNamespace

import pytest

from app.models.file_attachment import FileAttachment
from app.services.template_asset_seed import _seed_geometry, load_brand_manifests, merge_manifests
from app.services.video_studio import quality, timeline
from app.services.video_studio.spec import QUALITY_PRESETS, output_size, parse_video_spec, validate_video_spec

_MERGED = merge_manifests(load_brand_manifests())
_FILL = re.compile(r'fill="([^"]+)"')


def _asset(container: str, filename: str):
    return next(asset for asset in _MERGED[container] if asset.filename == filename)


def _fills(svg: bytes) -> set[str]:
    return {value for value in _FILL.findall(svg.decode("utf-8")) if value != "none"}


@pytest.mark.parametrize("container", ["filigran-brand", "filigran-brand-film"])
def test_the_opencrq_logo_ships_as_a_vector_product_logo(container):
    for name in ("opencrq-logo.svg", "opencrq-logo-light.svg"):
        asset = _asset(container, name)
        assert (asset.role, asset.content_type) == ("product_logo", "image/svg+xml"), name
        assert "mark + wordmark" in asset.description, asset.description


def test_it_is_drawn_like_the_other_products_logos():
    """Its mark and its wordmark in the OpenCTI logo's two inks, in each scheme, at the same height."""
    for suffix in ("", "-light"):
        crq = _asset("filigran-brand", f"opencrq-logo{suffix}.svg").data
        cti = _asset("filigran-brand", f"opencti-logo{suffix}.svg").data
        assert _fills(crq) == _fills(cti) and len(_fills(crq)) == 2
        assert b'height="26"' in crq.split(b">", 1)[0] and b'height="26"' in cti.split(b">", 1)[0]
        # Vector paths only: no embedded raster.
        assert b"<image" not in crq and crq.count(b"<path") >= 8


def test_no_description_sends_an_agent_to_the_png_for_the_logo():
    descriptions = {asset.filename: asset.description for asset in _MERGED["filigran-brand"]}
    for name, description in descriptions.items():
        if name.endswith(".png"):
            continue
        assert "opencrq-logo.png" not in description and "opencrq-logo-light.png" not in description, name
    assert "use opencrq-logo.svg for the mark + wordmark" in descriptions["opencrq-mark.svg"]
    for suffix in ("", "-light"):
        kept = descriptions[f"opencrq-logo{suffix}.png"]
        assert "992x208" in kept and f"place opencrq-logo{suffix}.svg instead" in kept


class _Db:
    """The files of the pack, as the database holds them once seeded: their bytes' geometry stored with them."""

    def __init__(self, rows: dict[uuid.UUID, FileAttachment]) -> None:
        self.rows = rows

    async def get(self, model, key, **_options):
        return self.rows.get(key) if model is FileAttachment else None

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list), all=list, first=lambda: None)


def _seeded(filename: str) -> FileAttachment:
    asset = _asset("filigran-brand-film", filename)
    return FileAttachment(
        id=uuid.uuid4(),
        filename=filename,
        content_type=asset.content_type,
        size=len(asset.data),
        s3_object_name=f"template-asset/seed/{filename}",
        image_meta=asyncio.run(_seed_geometry(asset)),
    )


@pytest.mark.parametrize("short_side", [QUALITY_PRESETS["high"].short_side, QUALITY_PRESETS["ultra"].short_side])
def test_an_image_scene_of_the_svg_passes_the_density_check_where_the_png_fails_it(short_side):
    png, svg = _seeded("opencrq-logo.png"), _seeded("opencrq-logo.svg")
    authored = validate_video_spec(
        {
            "title": "XTM Blast",
            "freestyle": True,
            "music": {"enabled": False},
            "scenes": [
                {"id": "crq-png", "type": "image", "file_id": str(png.id), "duration": 3},
                {"id": "crq-svg", "type": "image", "file_id": str(svg.id), "duration": 3},
            ],
        }
    )
    spec = parse_video_spec(authored)
    facts = asyncio.run(quality.content_facts(_Db({png.id: png, svg.id: svg}), spec, SimpleNamespace(id=uuid.uuid4())))
    facts.output = output_size(spec.format.aspect_ratio, short_side)
    found = quality.content_findings(spec, timeline.estimate_plan(spec, {}).document, facts, authored)
    soft = {finding.scene_id: finding for finding in found if finding.kind == "image_resolution"}
    assert set(soft) == {"crq-png"}, [f.message for f in found]
    assert soft["crq-png"].severity == "dense" and "its picture is 992x208" in soft["crq-png"].message
