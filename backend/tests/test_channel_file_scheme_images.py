"""``![alt](file:<uuid>)`` in a channel reply is signed like any image.

The file tools document this form for an image file of the conversation, and
models reuse it in answers. A channel cannot load a ``file:`` address, so the
reply points it at the file's download path first; ``resolve_channel_images``
then signs it under the conversation scope or keeps its alt text.
"""

from __future__ import annotations

import inspect
import uuid

from app.channels import router as channel_router
from app.channels.base import file_scheme_images_to_api, resolve_channel_images

_BASE = "https://xtm.example"
_MINE = str(uuid.uuid4())
_FOREIGN = str(uuid.uuid4())


def test_a_file_scheme_image_points_at_the_download_path():
    assert file_scheme_images_to_api(f"Scan:\n\n![WhatsApp pairing QR](file:{_MINE})") == (
        f"Scan:\n\n![WhatsApp pairing QR](/api/v1/chat/files/{_MINE}/download)"
    )


def test_case_angle_brackets_and_a_title_are_read():
    expected = f"![qr](/api/v1/chat/files/{_MINE}/download)"
    assert file_scheme_images_to_api(f"![qr](file:{_MINE.upper()})") == expected
    assert file_scheme_images_to_api(f"![qr](<file:{_MINE}>)") == expected
    assert file_scheme_images_to_api(f'![qr](file:{_MINE} "the code")') == expected


def test_anything_else_is_left_as_written():
    for text in (
        f"[[FILE:{_MINE}]]",
        f"[a link](file:{_MINE})",
        "![local](file:///Users/me/chart.png)",
        "![x](file:not-a-uuid)",
        f"![x](/api/v1/chat/files/{_MINE}/download)",
        "plain text",
        "",
    ):
        assert file_scheme_images_to_api(text) == text


def test_in_scope_it_is_signed_out_of_scope_it_keeps_its_alt_text():
    text = file_scheme_images_to_api(f"![qr](file:{_MINE}) ![secret](file:{_FOREIGN})")
    out, images = resolve_channel_images(text, _BASE, allowed_ids={_MINE})
    assert [(image.image_id, image.source) for image in images] == [(_MINE, "files")]
    assert f"![qr]({_BASE}/api/v1/chat/files/{_MINE}/public?token=" in out
    assert _FOREIGN not in out
    assert out.endswith(" secret")


def test_the_channel_reply_rewrites_before_it_scopes_and_signs():
    """Tripwire: the reply rewrites file: images before the produced-file
    markers (so the file is not linked twice) and before the image scope."""
    source = inspect.getsource(channel_router)
    rewrite = source.index("_outbound_source = file_scheme_images_to_api(_outbound_source)")
    assert rewrite < source.index("_outbound_source = _with_produced_file_markers(")
    assert rewrite < source.index("await channel_image_scope(")
