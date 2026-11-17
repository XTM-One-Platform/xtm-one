"""Keyed screen inserts on thin foreground edges (#10573, slice S1).

A hand or an animal muzzle in front of a green monitor carries the key's
spill on its edge as a skin or fur tone mixed with the key: yellow-green,
40-60 degrees off the key's hue, where the hue's weight alone corrected about
half of that edge. And the insert's light wrapped onto a finger thinner than
twice the wrap reached the core of what stands in front, where the insert is
never drawn: its blurred strokes read as the source drawn through it
(``insert_through_foreground``). What is proven:

* ``edge_limit`` corrects a spilled skin edge by colour difference
  (``screens.key_share``) whatever its hue, and leaves a clean skin tone alone;
* ``light_wrap`` never touches the core it is given, and is unchanged without one.

Slice S2: the refined matte and the gate's retry.

* ``guided_matte`` follows the footage's edge: a soft key band over a sharp
  subject edge comes back opaque on the subject's side;
* ``refined_front`` never lowers the subject's opacity, acts on the edge band
  alone and grows the core with what it makes opaque;
* ``on_edge`` tells hits on the boundary from hits inside the subject;
* ``compose`` draws a frame again refined only when it fails
  ``insert_through_foreground`` on the edge alone, the state put back first;
* the finding names the footage or the key's tolerance from the audits.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from scipy import ndimage

from app.services.video_studio import screen_insert, screens

KEY = screens.Key("green", (32, 255, 30))
SKIN = np.array((205.0, 150.0, 120.0), dtype=np.float32)


def _hand(size: int = 120) -> tuple[np.ndarray, np.ndarray]:
    """A disc of skin in front of the key: the mask of what stands in front, and the inside of the quad."""
    yy, xx = np.mgrid[0:size, 0:size]
    front = np.hypot(xx - size / 2, yy - size / 2) <= size / 4
    return front, np.ones(front.shape, dtype=bool)


def _spilled(front: np.ndarray, mix: float) -> np.ndarray:
    """The footage: the key around, skin inside, its outer ring the skin mixed with *mix* of the key."""
    colours = np.empty((*front.shape, 3), dtype=np.float32)
    colours[...] = np.asarray(KEY.rgb, dtype=np.float32)
    colours[front] = SKIN
    ring = front & (ndimage.distance_transform_edt(front) <= 3.0)
    colours[ring] = SKIN * (1.0 - mix) + np.asarray(KEY.rgb, dtype=np.float32) * mix
    return colours


def test_a_skin_edge_mixed_with_the_key_is_corrected_whatever_its_hue():
    front, inside = _hand()
    ring = front & (ndimage.distance_transform_edt(front) <= 3.0)
    for mix in (0.15, 0.25, 0.35):
        colours = _spilled(front, mix)
        out, share = screen_insert.edge_limit(
            colours, colours, front, inside, KEY, screen_insert.Despill(width_px=8.0), 6.0
        )
        assert share is not None
        lead = screens.key_share(out[ring], KEY)
        # The key's lead left on that edge: none past 2% of it.
        assert float((lead > screen_insert.EDGE_SPILL_LEAD).mean()) <= 0.02, mix


def test_a_clean_skin_edge_is_left_as_it_is():
    front, inside = _hand()
    colours = _spilled(front, 0.0)
    out, _share = screen_insert.edge_limit(
        colours, colours, front, inside, KEY, screen_insert.Despill(width_px=8.0), 6.0
    )
    assert np.allclose(out[front], colours[front], atol=1.0)


def test_the_light_wrap_never_touches_the_core_it_is_given():
    # A finger 5 px wide in front of a static keyed insert, whose core is the occlusion's (a 1 px feather: 1 px deep):
    # the 2 px wrap reaches it.
    front = np.zeros((60, 60), dtype=bool)
    front[:, 28:33] = True
    inside = np.ones(front.shape, dtype=bool)
    colours = np.empty((60, 60, 3), dtype=np.float32)
    colours[...] = SKIN
    behind = np.zeros_like(colours)
    behind[::4] = 255.0  # the insert's rows: strokes to wrap in
    core = screen_insert.front_core(front, inside, 1)
    assert core.any()
    wrapped = screen_insert.light_wrap(colours, behind, front, inside, 1.0, core)
    assert np.array_equal(wrapped[core], colours[core])
    plain = screen_insert.light_wrap(colours, behind, front, inside, 1.0)
    assert not np.array_equal(plain[core], colours[core]), "without a core the wrap reaches the finger's middle"
    edge = front & ~core
    assert np.array_equal(wrapped[edge], plain[edge]), "the edge band takes the same light"


def _plate(size: int = 80) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A finger 9 px wide, bright, over a dark key: its luma, its mask, and a key matte whose band is 4 px soft."""
    front = np.zeros((size, size), dtype=bool)
    front[:, 36:45] = True
    luma = np.where(front, 200.0, 60.0).astype(np.float32)
    depth_out = ndimage.distance_transform_edt(~front)
    depth_in = ndimage.distance_transform_edt(front)
    # The key's share: 1 on the surface, falling to 0 four pixels into the finger - its edge half seen through.
    matte = np.where(front, np.clip(1.0 - depth_in / 4.0, 0.0, 1.0), np.clip(1.0 - (depth_out - 1.0) / 4.0, 0.5, 1.0))
    return luma, front, matte.astype(np.float32)


def test_the_guided_matte_follows_the_footage_edge():
    luma, front, matte = _plate()
    opacity = 1.0 - matte
    refined = screen_insert.guided_matte(luma, opacity, 3)
    rim = front & (ndimage.distance_transform_edt(front) <= 2.0)
    assert float(refined[rim].mean()) > float(opacity[rim].mean()) + 0.2
    assert refined.min() >= 0.0 and refined.max() <= 1.0


def test_the_refined_front_raises_the_edge_alone_and_grows_the_core():
    luma, front, matte = _plate()
    inside = np.ones(front.shape, dtype=bool)
    region = np.dstack([luma, luma, luma])
    opacity = (1.0 - matte).astype(np.float32)
    core = screen_insert.front_core(front, inside, 4)
    raised, grown = screen_insert.refined_front(region, opacity, front, inside, core, 1.0)
    assert (raised >= opacity - 1e-6).all(), "never lowered"
    far = ndimage.distance_transform_edt(~front) > screen_insert.REFINED_MATTE_PX + 1.0
    assert np.array_equal(raised[far], opacity[far]), "the surface past the band is untouched"
    assert (grown >= core).all() and grown.sum() > core.sum()
    assert not (grown & ~front & far).any()


def test_on_edge_tells_the_boundary_from_the_inside():
    front = np.zeros((200, 200), dtype=bool)
    front[20:180, 20:180] = True
    rim = np.zeros_like(front)
    rim[22:26, 50:60] = True
    deep = np.zeros_like(front)
    deep[95:105, 95:105] = True
    assert screen_insert.on_edge(rim, front, 1.0)
    assert not screen_insert.on_edge(deep, front, 1.0)
    assert not screen_insert.on_edge(rim | deep, front, 1.0)
    assert not screen_insert.on_edge(np.zeros_like(front), front, 1.0)


def _layer(through: float | None, edge: bool | None, refinable: bool = True, refined: bool | None = None):
    audit = screen_insert.FrameAudit(through=through, through_edge=edge, matte_refined=refined)
    return SimpleNamespace(audit=audit, refinable=refinable)


def test_compose_draws_again_refined_only_on_an_edge_failure(monkeypatch):
    calls: list[bool] = []
    answers: dict[bool, object] = {}

    def fake(insert, picture, size, footage_frame=None, *, refine=False, **_kwargs):
        calls.append(refine)
        return answers[refine]

    monkeypatch.setattr(screen_insert, "_compose", fake)
    failing = screen_insert.MAX_THROUGH_SHARE * 3

    # A pass: drawn once.
    answers[False] = _layer(0.0, None)
    assert screen_insert.compose(None, None, (16, 9)) is answers[False]
    assert calls == [False]

    # A failure inside the subject: no retry, the tolerance is the advice.
    calls.clear()
    answers[False] = _layer(failing, False)
    assert screen_insert.compose(None, None, (16, 9)) is answers[False]
    assert calls == [False]

    # A failure on the edge of an unkeyed or filled layer: nothing to refine.
    calls.clear()
    answers[False] = _layer(failing, True, refinable=False)
    assert screen_insert.compose(None, None, (16, 9)) is answers[False]
    assert calls == [False]

    # A failure on the edge of a keyed layer: drawn again refined, that drawing kept.
    calls.clear()
    answers[False] = _layer(failing, True)
    answers[True] = _layer(0.0, None, refinable=False, refined=True)
    assert screen_insert.compose(None, None, (16, 9)) is answers[True]
    assert calls == [False, True]


def test_compose_puts_the_state_back_before_the_retry(monkeypatch):
    state = SimpleNamespace(last=(4, "m4"), before=(3, "m3"), refiner=SimpleNamespace(kept=[1]), drift=None)
    seen: list[tuple] = []

    def fake(insert, picture, size, footage_frame=None, *, refine=False, state=None, **_kwargs):
        seen.append((state.last, state.before, list(state.refiner.kept)))
        state.last, state.before = (5, "m5"), state.last
        state.refiner.kept.append(2)
        return _layer(screen_insert.MAX_THROUGH_SHARE * 3, True, refinable=not refine)

    monkeypatch.setattr(screen_insert, "_compose", fake)
    screen_insert.compose(None, None, (16, 9), state=state)
    assert seen == [((4, "m4"), (3, "m3"), [1]), ((4, "m4"), (3, "m3"), [1])]


def test_the_audit_carries_the_retry():
    audit = screen_insert.FrameAudit(through=0.002, through_edge=True, matte_refined=True)
    read = audit.as_dict()
    assert read["through_edge"] is True and read["matte_refined"] is True
    assert screen_insert.FrameAudit.from_dict(read).matte_refined is True


def test_the_finding_names_the_footage_or_the_tolerance():
    from app.services.video_studio import insert_checks

    keyed = SimpleNamespace(keyed=True)
    footage = insert_checks._through_cause(keyed, [{"matte_refined": True, "through_edge": True}])
    assert "footage" in footage and "no key setting" in footage
    inside = insert_checks._through_cause(keyed, [{"through_edge": False}])
    assert "key.tolerance" in inside
    assert insert_checks._through_cause(keyed, [{}]) == ""
    assert insert_checks._through_cause(SimpleNamespace(keyed=False), [{"through_edge": False}]) == ""
