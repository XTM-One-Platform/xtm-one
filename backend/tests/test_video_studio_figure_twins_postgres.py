"""A figure's template assets read as twins against a scratch Postgres.

The two reads ``figure_twins`` makes - the pictures of a slide that are
template assets, then every picture of their template - and the file access
rule a twin passes before it is drawn, run on real rows: a twin of a template
the person cannot see is never drawn, and a file that is no template asset
has none.
"""

from __future__ import annotations

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.file_attachment import FileAttachment
from app.models.template_container import TEMPLATE_ASSET_SOURCE, TemplateContainer
from app.models.user import User
from app.services.video_studio import figure_twins
from app.services.video_studio.figure_twins import Tone
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]

#: The tones the two drawings of each logo are read at (``image_geometry.image_tone``), by file name.
TONES = {
    "opencti-logo.svg": Tone(0.84, False, 4.57),
    "opencti-logo-light.svg": Tone(0.02, False, 4.57),
    "openaev-logo.svg": Tone(0.87, False, 5.09),
    "openaev-logo-light.svg": Tone(0.02, False, 5.09),
}


def _asset(container: TemplateContainer, uploader: User, filename: str, description: str) -> FileAttachment:
    return FileAttachment(
        user_id=uploader.id,
        template_container_id=container.id,
        source=TEMPLATE_ASSET_SOURCE,
        asset_role="product_logo",
        filename=filename,
        description=description,
        content_type="image/svg+xml",
        size=2048,
        s3_object_name=f"templates/{container.id}/{filename}",
    )


async def _slide(db) -> tuple[dict, dict[str, FileAttachment], dict[str, User]]:
    """A light deck's architecture over a brand template everyone sees, an admin's own one and an upload."""
    admin = User(email="admin@filigran.io", hashed_password="x", full_name="Admin", role="admin")
    author = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
    db.add_all([admin, author])
    await db.flush()
    brand = TemplateContainer(name="Brand deck", is_company_managed=True, user_id=None)
    private = TemplateContainer(name="Admin's draft deck", user_id=admin.id)
    db.add_all([brand, private])
    await db.flush()
    files = {
        "cti": _asset(brand, admin, "opencti-logo.svg", "OpenCTI logo (mark + wordmark), light-on-dark."),
        "cti_light": _asset(
            brand,
            admin,
            "opencti-logo-light.svg",
            "OpenCTI logo, dark-on-light - use on the light theme; opencti-logo.svg is the light-on-dark twin.",
        ),
        "aev": _asset(private, admin, "openaev-logo.svg", "OpenAEV logo (mark + wordmark), light-on-dark."),
        "aev_light": _asset(
            private, admin, "openaev-logo-light.svg", "OpenAEV logo, dark-on-light; openaev-logo.svg is the light-on-dark twin."
        ),
        "upload": FileAttachment(
            user_id=author.id,
            filename="opencti-logo.svg",
            content_type="image/svg+xml",
            size=10,
            s3_object_name="uploads/opencti-logo.svg",
        ),
    }
    db.add_all(list(files.values()))
    await db.commit()
    nodes = [
        {"id": "cti", "label": "Intel", "logo_file_id": str(files["cti_light"].id)},
        {"id": "aev", "label": "Validation", "logo_file_id": str(files["aev_light"].id)},
        {"id": "own", "label": "Upload", "logo_file_id": str(files["upload"].id)},
    ]
    return {"layout": "content", "blocks": [{"type": "architecture", "nodes": nodes}]}, files, {"admin": admin, "author": author}


async def test_a_twin_is_read_off_its_template_and_drawn_only_for_who_may_read_it(monkeypatch):
    async def tone_of(asset):
        return TONES[asset.filename]

    monkeypatch.setattr(figure_twins, "_tone_of", tone_of)
    async with (
        scratch_database("figure_twins", tables=ALL_TABLES, extensions=("vector",)) as scratch,
        scratch.sessions() as db,
    ):
        slide, files, users = await _slide(db)
        twins = await figure_twins.scheme_twins(db, slide, scheme="dark", user=users["author"])
        # The brand template is everyone's: its twin is drawn. The admin's own template is not the author's: its twin is
        # never drawn. An upload is no template asset: it has no twin.
        assert {raw: twin.twin for raw, twin in twins.items()} == {str(files["cti_light"].id): str(files["cti"].id)}
        # An admin reads every template: both are drawn.
        twins = await figure_twins.scheme_twins(db, slide, scheme="dark", user=users["admin"])
        assert {twin.twin_filename for twin in twins.values()} == {"opencti-logo.svg", "openaev-logo.svg"}
        # A light video draws the slide as it is.
        assert await figure_twins.scheme_twins(db, slide, scheme="light", user=users["admin"]) == {}
