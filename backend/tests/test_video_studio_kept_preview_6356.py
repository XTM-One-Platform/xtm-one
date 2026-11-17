"""What a scene preview is made from, and the encode that serves a kept one.

A preview whose every frame its last preview already drew is served from it (``kept_previews``). What is proven
here, without a database (the lane, against PostgreSQL, is ``test_video_studio_kept_preview_6356_postgres.py``):

* what a preview is made from moves with what it shows and what it says - its window, the scene's drawing, the
  narrations heard in it -, its quality, the person it is drawn for, the template it wears, and nothing else;
* the encode a kept preview is served with copies its streams into a file of its own, every frame kept;
* the checks it carries name the copies of the files they named, never the kept preview's own.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import encoder, kept_previews, render_checks

SPEC = {
    "title": "Brand film",
    "freestyle": True,
    "scenes": [
        {"id": "s1", "type": "title", "title": "Hook"},
        {"id": "fig", "type": "title", "title": "What we do"},
        {"id": "s3", "type": "title", "title": "Close"},
        {"id": "s4", "type": "title", "title": "After"},
    ],
}
USER = SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000e356"), role="user")


@pytest.fixture
def made_from(monkeypatch):
    """:func:`kept_previews.inputs` over a database stood in for: the scene digests and the voices are given."""
    from app.services.video_studio import lexicon_store, projects, reuse_index

    seen = {"whole": {"s1": "w1", "fig": "wfig", "s3": "w3", "s4": "w4"}, "voices": {}}

    async def digests(_db, effective, _lexicon=None):
        return render_checks.SceneDigests(dict(seen["whole"]), {key: f"c-{value}" for key, value in seen["whole"].items()})

    async def estimate(_db, effective, **_kwargs):
        return SimpleNamespace(document={"scenes": [{"id": s["id"], "start": 4.0 * i} for i, s in enumerate(effective["scenes"])]})

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render_checks, "digest_scenes", digests)
    monkeypatch.setattr(projects, "estimate", estimate)
    monkeypatch.setattr(lexicon_store, "for_voiceover", nothing)
    monkeypatch.setattr(reuse_index, "voice_target", nothing)
    monkeypatch.setattr(reuse_index, "voice_digests", lambda _target, _spec, _lexicon: dict(seen["voices"]))

    def of(spec: dict = SPEC, *, scene_id: str = "fig", quality: str = "standard", user=USER, template=None):
        return asyncio.run(kept_previews.inputs(None, spec, template, scene_id=scene_id, quality=quality, user=user))

    return SimpleNamespace(of=of, seen=seen)


def test_what_a_preview_is_made_from_moves_with_what_it_draws_and_says(made_from):
    first = made_from.of()
    assert first is not None and first == made_from.of()
    assert first.whole == {"fig": "wfig"} and first.content == {"fig": "c-wfig"}
    # Its quality, its person: other frames, another reader's pictures.
    assert made_from.of(quality="draft").digest != first.digest
    assert made_from.of(user=SimpleNamespace(id=uuid.uuid4(), role="user")).digest != first.digest
    # A neighbour its window shows, edited: another window.
    edited = {**SPEC, "scenes": [{**SPEC["scenes"][0], "title": "Hook!"}, *SPEC["scenes"][1:]]}
    assert made_from.of(edited).digest != first.digest
    # A scene its window never shows changes nothing.
    beyond = {**SPEC, "scenes": [*SPEC["scenes"][:3], {**SPEC["scenes"][3], "title": "Later"}]}
    assert made_from.of(beyond).digest == first.digest
    # What the scene plays (a take made again, a deck edited): its drawing's digest.
    made_from.seen["whole"]["fig"] = "wfig-2"
    assert made_from.of().digest != first.digest
    made_from.seen["whole"]["fig"] = "wfig"
    # A narration heard in its window, spoken again.
    made_from.seen["voices"] = {"s3": "voice-2"}
    assert made_from.of().digest != first.digest
    made_from.seen["voices"] = {"s4": "voice-2"}
    assert made_from.of().digest == first.digest
    # A scene the video does not have is made from nothing.
    assert made_from.of(scene_id="gone") is None


def test_a_kept_previews_checks_name_the_copies_of_its_files():
    proof, crop, strip = (str(uuid.uuid4()) for _ in range(3))
    checks = {
        "findings": [{"kind": "insert_halo", "severity": "warn", "at": 1.0, "message": "halo", "data": {"frame_file_id": proof}}],
        "inserts": [{"key": "fig:0", "crops": [{"word": "first", "file_id": crop}], "halo_crop": {"file_id": strip, "at": 1.0}}],
    }
    assert kept_previews._file_ids(checks) == [proof, crop, strip]
    copies = {proof: "p2", crop: "c2"}
    renamed = kept_previews._renamed(checks, copies)
    assert renamed["findings"][0]["data"] == {"frame_file_id": "p2"}
    assert renamed["inserts"][0]["crops"] == [{"word": "first", "file_id": "c2"}]
    # A file that could not be copied is named no more.
    assert renamed["inserts"][0]["halo_crop"] == {"at": 1.0}
    # Read back as the render stores them.
    carried = render_checks.Checks.from_dict(renamed)
    assert [f.data for f in carried.findings] == [{"frame_file_id": "p2"}] and carried.as_dict()["status"] == render_checks.FINDINGS


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_the_encode_copies_every_frame_into_a_file_of_its_own(tmp_path: Path):
    kept = tmp_path / "kept.mp4"
    asyncio.run(
        encoder.ffmpeg(
            ["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(kept)]
        )
    )
    out = tmp_path / "served.mp4"
    asyncio.run(encoder.remux(source_path=str(kept), out_path=str(out)))
    before, after = asyncio.run(encoder.probe(str(kept))), asyncio.run(encoder.probe(str(out)))
    assert after.frame_count == before.frame_count == 60 and after.has_audio and (after.width, after.height) == (320, 180)
