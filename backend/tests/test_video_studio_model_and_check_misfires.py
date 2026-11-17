"""A model's own provider, a locked camera said before it is paid for, and two checks that misfired.

* a model named alone is asked of the provider listing it - ``kling-v3`` never
  reaches the Gemini endpoint because Gemini is the video provider chosen in
  Settings - and a model several providers list is refused until one is named;
* a shot asking a locked-off camera of a model that does not hold one is
  asked of one that does, or the answer says so before the take is paid for;
* ``frozen``: a locked-off take whose only motion is a small subject is not
  frozen, a held frame still is;
* ``insert_padded``: a true-alpha picture placed with ``fit: contain`` on a
  tall quad holds no flat band, a card baked into a picture still does.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.llm import router
from app.llm.gemini_provider import GeminiProvider
from app.llm.kling_provider import KlingProvider
from app.services.video_studio import clips, frame_freeze, screen_insert

# ── A model's own provider ────────────────────────────────────────────────


def _config(provider: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, display_name=None)


class _Db:
    def __init__(self, configs):
        self.configs = configs

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))


class _Listing:
    """A provider listing *models* as its video models (``None``: its listing fails)."""

    def __init__(self, models):
        self.models = models

    async def list_video_models(self):
        if self.models is None:
            raise RuntimeError("the listing could not be read")
        return list(self.models)

    async def video_model_unavailable(self, _model):
        return None

    def max_reference_images(self, model):
        return 3 if model.startswith("kling") else 0

    def reference_images_with_start_frame(self, _model):
        return True


@pytest.fixture
def providers(monkeypatch):
    """The configured providers by config id, the platform's video choice on Gemini's Veo."""
    listed: dict = {}
    settings = SimpleNamespace(video_provider_id=None, video_model="veo-3.1-generate-preview")

    async def platform_settings(_db):
        return settings

    async def cached(config):
        return listed[config.id]

    async def allowed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(router, "_get_platform_settings", platform_settings)
    monkeypatch.setattr(router, "_cached_provider", cached)
    monkeypatch.setattr(router, "ensure_not_excluded", allowed)
    return SimpleNamespace(listed=listed, settings=settings)


def test_a_model_named_alone_is_asked_of_the_provider_listing_it_never_of_the_platforms(providers):
    gemini, kling = _config("gemini"), _config("kling")
    providers.listed.update({gemini.id: _Listing(["veo-3.1-generate-preview"]), kling.id: _Listing(["kling-v3", "kling-v2-6"])})
    providers.settings.video_provider_id = gemini.id
    _provider, model, config = asyncio.run(router.get_video_provider(_Db([gemini, kling]), model="kling-v3"))
    # The regeneration that named kling-v3 alone went to Gemini (HTTP 404 "models/kling-v3 is not found").
    assert (model, config) == ("kling-v3", kling)
    # The platform's own model, named, stays on its provider; nothing named, the platform's choice.
    assert asyncio.run(router.get_video_provider(_Db([gemini, kling]), model="veo-3.1-generate-preview"))[2] is gemini
    assert asyncio.run(router.get_video_provider(_Db([gemini, kling])))[2] is gemini
    # What a shot generated again is asked of: the model named, its provider resolved again, never the shot's own.
    assert clips._wanted_model({"provider_id": str(gemini.id)}, {"model": "kling-v3"}) == ("kling-v3", None)


def test_a_model_several_providers_list_is_refused_until_one_is_named(providers):
    kling, gateway = _config("kling"), _config("litellm")
    providers.listed.update({kling.id: _Listing(["kling-v3"]), gateway.id: _Listing(["kling-v3", "veo-3.1"])})
    with pytest.raises(ValueError, match=r"^The video model kling-v3 is offered by several providers \(kling, litellm\): name"):
        asyncio.run(router.get_video_provider(_Db([kling, gateway]), model="kling-v3"))
    _provider, model, config = asyncio.run(router.get_video_provider(_Db([kling, gateway]), model="kling-v3", provider_id=gateway.id))
    assert (model, config) == ("kling-v3", gateway)


def test_f53_a_model_several_providers_list_is_asked_of_the_video_s_provider_then_the_platform_s_and_said(providers):
    # b56997a2: veo-3.1-generate-001 named alone was refused (vertex_ai, elevenlabs), Vertex being the video's.
    vertex, eleven = _config("vertex_ai"), _config("elevenlabs")
    model = "veo-3.1-generate-001"
    providers.listed.update({vertex.id: _Listing([model]), eleven.id: _Listing(["veo-3.1-fast-generate-001", model])})
    db = _Db([vertex, eleven])
    # The video's shots of that model are made on Vertex AI: asked of it, and the answer names it and why.
    chose: list[str] = []
    prefer = [(str(vertex.id), clips.VIDEO_SHOTS_CHOICE)]
    _provider, chosen, config = asyncio.run(router.get_video_provider(db, model=model, prefer=prefer, chose=chose))
    assert (chosen, config) == (model, vertex)
    assert chose == [
        f"{model} is offered by vertex_ai, elevenlabs: asked of vertex_ai, the provider this video's shots of it are made "
        "on - another one is named with provider_id."
    ]
    # The early check reads its limits where it is asked.
    assert asyncio.run(router.declared_video_reference_limits(db, model=model, prefer=prefer))[0] == model
    # A video with none of its own: the platform's provider, Vertex AI in Settings.
    providers.settings.video_provider_id, providers.settings.video_model = vertex.id, model
    chose.clear()
    assert asyncio.run(router.get_video_provider(db, model=model, chose=chose))[2] is vertex
    assert chose[0].endswith(f"asked of vertex_ai, {router.PLATFORM_VIDEO_CHOICE} - another one is named with provider_id.")
    # A preference no provider of the model is passes to the platform's; one naming ElevenLabs takes it.
    assert asyncio.run(router.get_video_provider(db, model=model, prefer=[(str(uuid.uuid4()), "gone")]))[2] is vertex
    assert asyncio.run(router.get_video_provider(db, model=model, prefer=[(str(eleven.id), clips.OWN_SHOT_CHOICE)]))[2] is eleven
    # The provider named is the one asked, whatever the preference.
    assert asyncio.run(router.get_video_provider(db, model=model, provider_id=eleven.id, prefer=prefer))[2] is eleven


def test_f53_the_video_s_provider_for_a_model_is_read_off_its_shots_of_that_model(monkeypatch):
    from app.models.video_studio import VideoClip
    from app.services.video_studio import projects

    vertex, eleven = str(uuid.uuid4()), str(uuid.uuid4())
    shots = [
        VideoClip(id=uuid.uuid4(), provider="vertex_ai", model="veo-3.1-generate-001", prompt="p", params={"provider_id": vertex}),
        VideoClip(id=uuid.uuid4(), provider="elevenlabs", model="veo-3.1-fast-generate-001", prompt="p", params={"provider_id": eleven}),
    ]
    video = SimpleNamespace(spec={"scenes": [{"id": f"s{n}", "type": "clip", "clip_id": str(shot.id)} for n, shot in enumerate(shots)]})
    asked: list[str] = []

    class _Shots:
        async def execute(self, statement):
            asked.append(str(statement))
            wanted = [shot for shot in shots if shot.model == "veo-3.1-generate-001"]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: wanted))

    async def visible(_db, _video_id, _user):
        return video

    monkeypatch.setattr(projects, "load_visible_project", visible)
    user = SimpleNamespace(id=uuid.uuid4())
    found = asyncio.run(clips.video_providers(_Shots(), user, uuid.uuid4(), "veo-3.1-generate-001"))
    assert found == [(vertex, clips.VIDEO_SHOTS_CHOICE)] and "video_clips.model = " in asked[0]
    # No video, or no model named: nothing read.
    assert asyncio.run(clips.video_providers(_Shots(), user, None, "veo-3.1-generate-001")) == []
    assert asyncio.run(clips.video_providers(_Shots(), user, uuid.uuid4(), None)) == [] and len(asked) == 1


def test_a_model_no_provider_lists_goes_to_one_serving_unlisted_models_or_is_refused(providers):
    gemini, gateway, kling = _config("gemini"), _config("litellm"), _config("kling")
    providers.listed.update({gemini.id: _Listing(["veo-3.1-generate-preview"]), gateway.id: _Listing([]), kling.id: _Listing(None)})
    providers.settings.video_provider_id = gemini.id
    # Listed by no one, every listing read: refused, never sent to the platform's provider.
    with pytest.raises(ValueError, match="No video provider you can use offers the model kling-v9"):
        asyncio.run(router.get_video_provider(_Db([gemini]), model="kling-v9"))
    # A gateway listing nothing serves the models its admin deployed.
    assert asyncio.run(router.get_video_provider(_Db([gemini, gateway]), model="vertex/my-veo"))[2] is gateway
    # A provider whose listing failed may still be the one: kling-v3 is asked of Kling, never of Gemini.
    assert asyncio.run(router.get_video_provider(_Db([gemini, kling]), model="kling-v3"))[2] is kling


def test_a_models_limits_are_read_on_the_provider_listing_it(providers):
    gemini, kling = _config("gemini"), _config("kling")
    providers.listed.update({gemini.id: _Listing(["veo-3.1-generate-preview"]), kling.id: _Listing(["kling-v3"])})
    providers.settings.video_provider_id = gemini.id

    def declared(**kwargs):
        return asyncio.run(router.declared_video_reference_limits(_Db([gemini, kling]), **kwargs))

    # Never Gemini's limits for Kling's model: the early check of a shot asks the model's own provider.
    assert declared(model="kling-v3") == ("kling-v3", {"reference_images": 3, "reference_images_with_start_frame": True})
    assert declared(model="veo-3.1-generate-preview")[1]["reference_images"] == 0


# ── A locked camera, said before it is paid for ───────────────────────────


class _Camera:
    def __init__(self, holds):
        self.holds = holds

    def holds_locked_camera(self, _model, *, start_frame):
        return self.holds(start_frame)


@pytest.fixture
def cameras(monkeypatch):
    veo, kling = SimpleNamespace(id=uuid.uuid4(), provider="gemini"), SimpleNamespace(id=uuid.uuid4(), provider="kling")
    found = {
        (str(veo.id), "veo-3.1-generate-preview"): (_Camera(lambda start: start), "veo-3.1-generate-preview", veo),
        (str(kling.id), "kling-v3"): (_Camera(lambda _start: True), "kling-v3", kling),
    }

    async def listed(_db, *, user_id):
        return [{"provider_id": provider_id, "model": model} for provider_id, model in found]

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return found[(str(provider_id), model)]

    monkeypatch.setattr(router, "list_video_models_for", listed)
    monkeypatch.setattr(clips, "resolve", resolve)
    return SimpleNamespace(veo=found[(str(veo.id), "veo-3.1-generate-preview")], kling=found[(str(kling.id), "kling-v3")])


def _locked(resolved, **kwargs):
    return asyncio.run(clips.locked_camera(None, SimpleNamespace(id=uuid.uuid4()), resolved, **kwargs))


def test_a_locked_camera_the_model_does_not_hold_stays_on_it_and_the_answer_names_one_that_does(cameras):
    # One model per round: the platform's model is never swapped for one that holds the camera.
    for tolerance in (0.3, 0.5):
        assert _locked(cameras.veo, tolerance=tolerance, start_frame=False) == [
            "veo-3.1-generate-preview does not hold a locked-off camera from a prompt without a start frame: with "
            f"max_camera_motion {tolerance:.2f}, its take is likely to fail its camera check and be paid for anyway - "
            "give it a start frame (reference_*) or name kling-v3, which holds one."
        ]


def test_a_model_that_holds_the_camera_or_a_camera_free_to_move_says_nothing(cameras):
    # With a start frame, the same model holds it; over 0.5 the camera is free to move; a model that does not say.
    assert _locked(cameras.veo, tolerance=0.3, start_frame=True) == []
    assert _locked(cameras.veo, tolerance=0.6, start_frame=False) == []
    assert _locked(cameras.veo, tolerance=None, start_frame=False) == []
    unknown = (_Camera(lambda _start: None), "gen4.5", SimpleNamespace(id=uuid.uuid4(), provider="runway"))
    assert _locked(unknown, tolerance=0.2, start_frame=False) == []


def test_the_providers_say_which_of_their_models_hold_a_locked_camera():
    gemini = GeminiProvider.__new__(GeminiProvider)
    assert gemini.holds_locked_camera("veo-3.1-generate-preview", start_frame=False) is False
    assert gemini.holds_locked_camera("veo-3.1-generate-preview", start_frame=True) is True
    kling = KlingProvider("api-key-kling-test")
    assert kling.holds_locked_camera("kling-v3", start_frame=False) is True
    assert kling.holds_locked_camera("not-a-kling-model", start_frame=False) is None


# ── frozen ────────────────────────────────────────────────────────────────


def _corridor() -> np.ndarray:
    """A grey copy (192 wide) of a locked-off corridor: walls, a floor, no motion of their own."""
    rows, columns = np.mgrid[0:108, 0:192]
    picture = 60 + 0.4 * rows + 30 * np.sin(columns / 9.0)
    return np.clip(picture, 0, 255).astype(np.uint8)


def test_a_locked_off_take_whose_only_motion_is_a_small_subject_is_not_frozen():
    corridor = _corridor()
    frames = []
    for index in range(45):
        frame = corridor.copy()
        # A cat far down the corridor: 3 x 3 pixels of the copy, a pixel further every three frames.
        x = 110 + index // 3
        frame[70:73, x : x + 3] = 15
        frames.append(frame)
    # The mean reads it as still: what reported 0.5-1.0 s frozen windows.
    assert float(np.abs(frames[0].astype(np.int16) - frames[2]).mean()) <= frame_freeze.SAME_DIFFERENCE
    assert frame_freeze.frozen_runs(frames, 30.0) == []
    # A frame held is frozen as before, a subject that stops moving too.
    assert frame_freeze.frozen_runs([corridor] * 30, 30.0) == [(0, 30)]
    assert frame_freeze.frozen_runs(frames[:9] + [frames[9]] * 21, 30.0) == [(9, 30)]


# ── insert_padded ─────────────────────────────────────────────────────────


def _alpha_mark() -> Image.Image:
    """A logo on true alpha, mostly transparent: an emblem, a wordmark, a soft halo fading into the surface."""
    mark = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    # The halo spans the picture's width at its top and bottom, half transparent: a glow, never a card.
    draw.rectangle((0, 0, 599, 40), fill=(255, 255, 255, 150))
    draw.rectangle((0, 259, 599, 299), fill=(255, 255, 255, 150))
    draw.ellipse((20, 70, 180, 230), fill=(0, 180, 255, 255))
    draw.rectangle((210, 120, 580, 180), fill=(255, 255, 255, 255))
    return mark


def test_a_true_alpha_picture_on_a_tall_quad_in_a_contain_fit_holds_no_flat_band():
    mark = _alpha_mark()
    alpha = np.asarray(mark)[..., 3]
    assert float((alpha < 255).mean()) > 0.68
    # A tall quad: contain pads it top and bottom with transparency, the surface showing there.
    assert screen_insert.padded_sides(mark, "contain", 300, 900) is None
    assert screen_insert.padded_sides(mark, "contain", 900, 300) is None
    assert screen_insert.padded_sides(mark, "cover", 600, 300) is None


def test_a_card_baked_into_a_picture_is_padded_whatever_quad_its_contain_fit_pads():
    # A white card letterboxed in black: the black bands are not the card's own sides.
    card = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 90, 399, 209), fill=(255, 255, 255, 255))
    draw.rectangle((160, 130, 240, 170), fill=(200, 30, 40, 255))
    assert screen_insert.padded_sides(card, "contain", 400, 300) == "top and bottom"
    # The transparent letterbox of a tall quad no longer hides the card's own bands.
    assert screen_insert.padded_sides(card, "contain", 200, 900) == "top and bottom"
    # A black plate with a white bar on it is one surface with a mark, never a card.
    plate = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    ImageDraw.Draw(plate).rectangle((40, 120, 360, 180), fill=(255, 255, 255, 255))
    assert screen_insert.padded_sides(plate, "contain", 400, 300) is None
