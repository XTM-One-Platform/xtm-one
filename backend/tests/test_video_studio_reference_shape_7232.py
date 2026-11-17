"""A reference image of a shape the video model refuses, caught before anything is paid.

Synthetic pictures only: a wide transparent wordmark-like PNG (1200x200, 6:1),
a square one, an SVG drawn as a 6:1 band. The model's limits are what its
provider declares (``reference_image_limits``); a model declaring none is
listed as unverified and nothing is refused on our side.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.llm.video import ReferenceImageLimits, VideoRequestRefusedError, ratio_label, status_error
from app.models.file_attachment import FileAttachment
from app.services.video_studio import assets, clips

USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000c232"), role="user")
WIDE = "00000000-0000-0000-0000-00000000c201"
SQUARE = "00000000-0000-0000-0000-00000000c202"
VECTOR = "00000000-0000-0000-0000-00000000c203"
LIMITS = ReferenceImageLimits(min_ratio=1 / 2.5, max_ratio=2.5, min_side=300, formats=("image/png", "image/jpeg"), max_count=3)


def _png(size: tuple[int, int], *, mode: str = "RGBA") -> bytes:
    picture = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else (40, 40, 40))
    # A white band where the word would be: every pixel of it must survive the padding.
    for x in range(size[0] // 10, size[0] - size[0] // 10):
        for y in range(size[1] // 3, 2 * size[1] // 3):
            picture.putpixel((x, y), (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255))
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="200"><rect x="100" y="60" width="1000" height="80" fill="#fff"/></svg>'
STORED = {WIDE: (_png((1200, 200)), "image/png"), SQUARE: (_png((600, 600)), "image/png"), VECTOR: (SVG, "image/svg+xml")}


class _Omni:
    """Declares Kling 3.0 Omni's reference images: three, with a start frame, of :data:`LIMITS`."""

    def max_reference_images(self, model):
        return 3

    def reference_images_with_start_frame(self, model):
        return True

    def reference_image_limits(self, model):
        return LIMITS


class _Undocumented(_Omni):
    def reference_image_limits(self, model):
        return None


class _Files:
    async def get(self, _model, key):
        data, mime = STORED[str(key)]
        return FileAttachment(id=key, filename="mark", content_type=mime, size=len(data), s3_object_name=str(key))


@pytest.fixture
def stored(monkeypatch):
    from app.services import file_access
    from app.services.file_storage import file_storage

    async def can_read(_row, _user, _db):
        return True

    async def download(name):
        return STORED[str(name)][0]

    monkeypatch.setattr(file_access, "can_read_file", can_read)
    monkeypatch.setattr(file_storage, "download", download)


def _resolve(ids, provider, *, fit=None, notes=None):
    return asyncio.run(
        clips.resolve_reference_images(
            _Files(), USER, ids, provider=provider, model="kling-3.0-omni", start_frame=True, fit=fit, notes=notes
        )
    )


def test_a_picture_outside_the_models_ratio_is_refused_by_name_before_anything_is_paid(stored):
    with pytest.raises(clips.ClipError) as refused:
        _resolve([WIDE], _Omni())
    said = str(refused.value)
    assert WIDE in said and "1200x200 (6.0:1)" in said and "1:2.5 to 2.5:1" in said and "kling-3.0-omni" in said
    assert 'reference_fit: "pad"' in said and "nothing was paid" in said
    # Inside the range: as before.
    assert _resolve([SQUARE], _Omni()) == [SQUARE]


def test_pad_keeps_the_mode_and_says_the_padded_shape(stored):
    notes: list[str] = []
    assert _resolve([WIDE], _Omni(), fit=clips.FIT_PAD, notes=notes) == [WIDE]
    assert notes == [f"Reference image `{WIDE}` padded from 6.0:1 (1200x200) to 2.5:1 (1200x480) for kling-3.0-omni."]


def test_the_padded_picture_keeps_every_pixel_of_the_mark():
    data, mime, note = asyncio.run(
        clips.fit_reference(_png((1200, 200)), "image/png", LIMITS, fit=clips.FIT_PAD, name="m", model=None)
    )
    assert mime == "image/png" and note and "to 2.5:1" in note
    with Image.open(io.BytesIO(data)) as padded:
        assert padded.size == (1200, 480) and padded.mode == "RGBA"
        # The band lands centred, untouched; the padding is transparent.
        assert padded.getpixel((600, 240)) == (255, 255, 255, 255)
        assert padded.getpixel((600, 5))[3] == 0
    # A small tall picture is padded to the ratio, then scaled to the model's smallest side.
    data, _mime, _note = asyncio.run(
        clips.fit_reference(_png((40, 200)), "image/png", LIMITS, fit=clips.FIT_PAD, name="m", model=None)
    )
    with Image.open(io.BytesIO(data)) as padded:
        assert min(padded.size) >= 300 and 1 / 2.5 - 0.01 <= padded.size[0] / padded.size[1] <= 2.5


def test_an_svg_is_rasterised_before_its_shape_is_checked_and_the_answer_says_so(stored):
    with pytest.raises(clips.ClipError, match=r"an SVG, rasterised to PNG first"):
        _resolve([VECTOR], _Omni())
    notes: list[str] = []
    _resolve([VECTOR], _Omni(), fit=clips.FIT_PAD, notes=notes)
    assert len(notes) == 1 and "an SVG, rasterised to PNG first" in notes[0] and "padded from 6.0:1" in notes[0]


def test_a_model_declaring_no_limit_refuses_nothing_and_is_listed_unverified(stored):
    from app.llm.router import video_reference_listing

    assert _resolve([WIDE], _Undocumented()) == [WIDE]
    assert video_reference_listing(_Undocumented(), "m")["reference_constraints"] == "unverified"
    listed = video_reference_listing(_Omni(), "kling-3.0-omni")["reference_constraints"]
    assert listed["aspect_ratio"]["range"] == "1:2.5 to 2.5:1" and listed["min_side_px"] == 300 and listed["max_count"] == 3


def test_kling_omni_declares_its_reference_limits():
    from app.llm.kling_provider import KlingProvider
    from app.llm.router import video_reference_listing

    kling = KlingProvider(api_key="k")
    assert kling.reference_image_limits("kling-3.0-omni") is not None
    assert kling.reference_image_limits("kling-v3") is None
    assert "reference_constraints" not in video_reference_listing(kling, "kling-v3")
    assert video_reference_listing(kling, "kling-3.0-omni")["reference_constraints"]["aspect_ratio"]["max"] == 2.5


def test_reference_fit_is_refuse_or_pad():
    assert clips.reference_fit_choice(None) is None and clips.reference_fit_choice("pad") == "pad"
    with pytest.raises(clips.ClipError, match='reference_fit is "refuse"'):
        clips.reference_fit_choice("stretch")


def test_the_worker_pads_the_picture_it_sends(monkeypatch, tmp_path):
    async def download(file_id, path):
        Path(path).write_bytes(STORED[WIDE][0])
        return SimpleNamespace(content_type="image/png")

    monkeypatch.setattr(assets, "download_file", download)

    def sent_for(params):
        read = asyncio.run(clips.reference_pictures(params, str(tmp_path)))
        return asyncio.run(clips.fit_reference_pictures(params, read, LIMITS, model="kling-3.0-omni"))

    with pytest.raises(clips.ClipError, match="outside the accepted aspect ratio"):
        sent_for({clips.REFERENCE_IMAGES: [WIDE]})
    # A model declaring nothing: sent as read.
    read = asyncio.run(clips.reference_pictures({clips.REFERENCE_IMAGES: [WIDE]}, str(tmp_path)))
    assert asyncio.run(clips.fit_reference_pictures({}, read, None, model="m")) == read
    sent = sent_for({clips.REFERENCE_IMAGES: [WIDE], clips.REFERENCE_FIT: clips.FIT_PAD})
    with Image.open(io.BytesIO(sent[0][0])) as padded:
        assert padded.size == (1200, 480) and sent[0][1] == "image/png"


def test_a_provider_refusal_on_the_arguments_is_unpaid_and_gives_the_try_back():
    refused = status_error(400, RuntimeError("Image aspect ratio is invalid"), "kling-3.0-omni")
    assert isinstance(refused, VideoRequestRefusedError) and refused.code == 400
    assert not isinstance(status_error(500, RuntimeError("boom"), "m"), VideoRequestRefusedError)
    # About a job that exists upstream: paid, never given back.
    assert not isinstance(status_error(400, RuntimeError("x"), "m", about_job=True), VideoRequestRefusedError)
    prompt = "A wordmark on a dark stage"
    params: dict = {}
    clips.count_regeneration(params, prompt=prompt, changes={})
    clips.count_regeneration(params, prompt=prompt, changes={})
    assert params[clips.REGENERATIONS]["count"] == 2
    assert clips.uncount_regeneration(params, prompt) is True
    assert params[clips.REGENERATIONS]["count"] == 1
    # Given back once only: a second refusal of the same round takes nothing more.
    assert clips.uncount_regeneration(params, prompt) is False and params[clips.REGENERATIONS]["count"] == 1
    # A round that counted nothing (a model change only) gives nothing back.
    clips.count_regeneration(params, prompt=prompt, changes={"model": "kling-v3"})
    assert clips.uncount_regeneration(params, prompt) is False


def test_ratio_label_reads_wide_and_tall():
    assert ratio_label(6.0) == "6.0:1" and ratio_label(2.5) == "2.5:1" and ratio_label(0.4) == "1:2.5"
    assert ratio_label(1200 / 480) == "2.5:1"
