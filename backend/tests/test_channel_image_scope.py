"""A channel reply signs only the images of its own conversation.

``resolve_channel_images`` turned every platform image address in an external
channel reply into a 24 h public link, and the adapters uploaded the bytes,
whatever conversation the file belonged to. It now signs only the ids its
caller vouches for (``channel_image_scope``, the scope
``resolve_channel_file_markers`` already applies); anything else keeps its alt
text and no link. The scope query itself is pinned on PostgreSQL in
``test_channel_image_scope_postgres.py``.
"""

from __future__ import annotations

import inspect
import uuid

from app.channels import base as channel_base
from app.channels import router as channel_router
from app.channels.base import channel_image_scope, resolve_channel_images

_BASE = "https://xtm.example"
_MINE = str(uuid.uuid4())
_IMAGE = str(uuid.uuid4())
_FOREIGN = str(uuid.uuid4())


def test_only_an_image_in_scope_is_signed():
    text = (
        f"Here: ![chart](/api/v1/chat/files/{_MINE}/download) "
        f"![fox](/api/v1/chat/images/{_IMAGE}) "
        f"![secret](/api/v1/chat/files/{_FOREIGN}/download)"
    )
    out, images = resolve_channel_images(text, _BASE, allowed_ids={_MINE, _IMAGE})

    assert [(image.image_id, image.source) for image in images] == [(_MINE, "files"), (_IMAGE, "images")]
    assert f"{_BASE}/api/v1/chat/files/{_MINE}/public?token=" in out
    assert f"{_BASE}/api/v1/chat/images/{_IMAGE}/public?token=" in out
    # Out of scope: its alt text, no link, no address, no upload.
    assert _FOREIGN not in out
    assert out.endswith(" secret")


def test_the_scope_is_compared_without_case():
    out, images = resolve_channel_images(
        f"![c](/api/v1/chat/files/{_MINE.upper()})", _BASE, allowed_ids={_MINE}
    )
    assert len(images) == 1 and "/public?token=" in out


def test_without_a_scope_nothing_is_signed():
    for allowed in (None, set()):
        out, images = resolve_channel_images(
            f"![chart](/api/v1/chat/files/{_MINE}/download) and ![](/api/v1/chat/images/{_IMAGE})",
            _BASE,
            allowed_ids=allowed,
        )
        assert images == []
        assert out == "chart and "


def test_text_without_an_image_address_is_untouched():
    text = "No image here, just [a link](https://example.com)."
    assert resolve_channel_images(text, _BASE, allowed_ids={_MINE}) == (text, [])


async def test_no_scope_and_no_address_never_query():
    class _NoDB:
        async def execute(self, _statement):  # pragma: no cover - must not run
            raise AssertionError("queried")

    assert await channel_image_scope(_NoDB(), f"![c](/api/v1/chat/files/{_MINE})") == set()
    assert await channel_image_scope(_NoDB(), "plain text", conversation_id=uuid.uuid4()) == set()


async def test_a_failed_scope_lookup_signs_nothing():
    class _BrokenDB:
        async def execute(self, _statement):
            raise RuntimeError("database down")

    text = f"![c](/api/v1/chat/files/{_MINE})"
    assert await channel_image_scope(_BrokenDB(), text, conversation_id=uuid.uuid4()) == set()


def test_the_channel_reply_signs_images_under_the_conversation_scope():
    """Tripwire: the one production caller passes the scope it looked up."""
    source = inspect.getsource(channel_router)
    call = source[source.index("resolve_channel_images(") :]
    call = call[: call.index(")\n")]
    assert "allowed_ids=" in call
    scope = source[source.index("await channel_image_scope(") :]
    scope = scope[: scope.index(")\n")]
    assert "conversation_id=conv.id if conv else None" in scope
    assert "user_id=user.id if user else None" in scope
    assert "allowed_ids" in inspect.signature(channel_base.resolve_channel_images).parameters
