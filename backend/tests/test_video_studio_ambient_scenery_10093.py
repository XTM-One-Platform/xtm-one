"""``legibility: ambient`` honoured on scenery (issue #10093).

An ambient screen insert is said under "Not judged, as the video declares" only. The #10036 override - a recording's
UI text under the illegible floor on an insert declared ambient - survives only where the insert cannot be scenery:
drawn at 0.3x or more, alone on its frame, not on a statement scene; and only as an advisory the render gate never
counts, whatever the template. A non-ambient insert at the same sizes is judged as before.
"""

from __future__ import annotations

import dataclasses

from app.models.video_studio import JOB_READY
from app.services.video_studio import insert_legibility, perceptual, quality, render_checks, timeline
from app.services.video_studio.insert_legibility import Legibility, TextSize
from app.services.video_studio.spec import parse_video_spec

CLIP = "00000000-0000-0000-0000-00000000c093"
TAKE = "00000000-0000-0000-0000-00000000a093"


def _surface(key: str, scale: float, *, ambient: bool = True, beside: int = 0, pane: int | None = None) -> Legibility:
    """A recording surface whose 12 px text is drawn at *scale* on the 1080p frame."""
    return Legibility(
        key,
        ("take", TAKE),
        (1920, 1080),
        1920 * scale,
        scale,
        TextSize(12.0, 60),
        viewport=1920.0,
        pane=pane,
        pixel_ratio=1.0,
        ambient=ambient,
        inserts_beside=beside,
    )


def test_two_ambient_wall_inserts_at_quarter_scale_are_never_listed():
    measured = [_surface("insert/s1.0", 0.25, beside=1), _surface("insert/s1.1", 0.25, beside=1)]
    assert all(item.illegible for item in measured)
    assert insert_legibility.ambient_overrides(measured) == []
    assert insert_legibility.findings(measured) == []


def test_an_ambient_insert_under_a_statement_is_never_listed():
    measured = [_surface("insert/s2.0", 0.20)]
    assert insert_legibility.ambient_overrides(measured, {"s2"}) == []
    # Under 0.3x it is scenery even outside a statement: a whole page on a wall screen.
    assert insert_legibility.ambient_overrides(measured) == []


def test_an_ambient_insert_sharing_its_frame_or_a_wall_pane_is_never_listed():
    # Two inserts on one scene, not side by side: they still share the frame.
    shared = [_surface("insert/s1.0", 0.4), _surface("insert/s1.1", 0.4)]
    assert insert_legibility.ambient_overrides(shared) == []
    assert insert_legibility.ambient_overrides([_surface("insert/s1.0.1", 0.4, pane=1)]) == []


def test_a_lone_large_ambient_recording_is_advice_naming_its_label_never_offered_ambient_again():
    [(scene_id, words, surface)] = insert_legibility.ambient_overrides([_surface("insert/s1.0", 0.4)])
    assert scene_id == "s1" and surface == f"insert/s1.0|take:{TAKE}"
    assert "marked legibility: ambient, but drawn alone on its frame at 0.40x" in words
    assert "the render does not wait for it" in words
    assert 'declare legibility: "ambient"' not in words
    # A file, or text that reads, is never overridden.
    readable = dataclasses.replace(_surface("insert/s1.0", 0.6), text=TextSize(24.0, 60))
    assert insert_legibility.ambient_overrides([readable]) == []


def test_non_ambient_inserts_at_those_sizes_keep_their_legibility_finding():
    measured = [
        _surface("insert/s1.0", 0.25, ambient=False, beside=1),
        _surface("insert/s1.1", 0.25, ambient=False, beside=1),
        _surface("insert/s2.0", 0.20, ambient=False),
    ]
    found = insert_legibility.findings(measured)
    assert [severity for _scene, _words, severity, _surface in found] == ["dense"] * 3
    assert all('declare legibility: "ambient"' in words for _scene, words, _severity, _surface in found)
    assert insert_legibility.ambient_overrides(measured) == []


def _spec() -> object:
    def insert(x: float) -> dict:
        return {
            "type": "screen_insert",
            "quad": [[x, 0.3], [x + 0.3, 0.3], [x + 0.3, 0.55], [x, 0.55]],
            "source": {"take_id": TAKE},
            "duration": 4,
            "legibility": "ambient",
        }

    return parse_video_spec(
        {
            "title": "Ops room",
            "freestyle": True,
            "scenes": [
                {"id": "s1", "type": "clip", "clip_id": CLIP, "overlays": [insert(0.1), insert(0.55)]},
                {"id": "s3", "type": "clip", "clip_id": CLIP, "overlays": [insert(0.3)]},
            ],
        }
    )


def _content(measured: list[Legibility]) -> list:
    spec = _spec()
    clip = timeline.ClipMedia(8.0, 1920, 1080, 24.0, 192)
    document = timeline.plan(spec, timeline.PlanMedia(clips={"s1": clip, "s3": clip}), fps=30, theme={}).document
    facts = quality.ContentFacts(
        shots={CLIP: quality.ShotFacts("Ops room", JOB_READY, width=1920, height=1080)},
        ready_takes={TAKE},
        legibility=measured,
    )
    return [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_legibility"]


def test_the_save_lists_no_ambient_wall_insert_and_the_render_gate_counts_no_override_on_a_brand_film(monkeypatch):
    # A brand film or product demo template: a perceptual finding blocks its render (``perceptual.blocks``).
    monkeypatch.setattr(perceptual, "blocks", lambda _purposes: True)
    walls = [_surface("insert/s1.0", 0.25, beside=1), _surface("insert/s1.1", 0.25, beside=1)]
    assert _content(walls) == []
    # A lone ambient recording drawn at 0.4x is advice even on a brand film, never a severity the gate blocks on.
    [found] = _content([*walls, _surface("insert/s3.0", 0.4)])
    assert found.scene_id == "s3" and found.severity == "advisory"
    assert found.severity not in render_checks.BLOCKING_SEVERITIES
    assert quality.QUALITY_REPORT_VERSION >= 55
