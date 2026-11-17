"""The compositor lays every frame out so nothing covers anything else, in a real Chromium.

The layout is a property of fonts, wrapping and pixels, so it is proven where
it happens: each document is played in the compositor page by
``quality.measure_document`` and its ``audit()`` must find nothing. Each
scenario is built so that it needs the mechanism its test names - with that
mechanism switched off, the audit reports the overlap:

* a card whose text reaches the bottom keeps clear of two lines of subtitles,
  burned in or left to the player (the subtitles' band);
* a wide lower third stands above the subtitles (the band, for overlays);
* a badge asked over a statement's kicker takes a free corner;
* two callouts on one element stack instead of covering each other;
* a word wider than the frame is scaled into it.

The audit is then shown to catch what no layout can fix: text still coming in
when its scene ends. And a video just created reads clean on every built-in
template: the cards a template adds stay on screen long enough to be read.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS
from app.services.video_studio import quality, templates, timeline
from app.services.video_studio.spec import authored_spec, parse_video_spec
from app.services.video_studio.themes import build_theme

pytestmark = pytest.mark.chromium


async def _audit(document: dict) -> dict:
    try:
        return await quality.measure_document(document)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _scene(sid: str, kind: str, start: float, end: float, **fields) -> dict:
    return {
        "id": sid,
        "type": kind,
        "transition_in": {"type": "cut", "duration": 0},
        "overlays": [],
        "start": start,
        "end": end,
        **fields,
    }


def _document(
    scenes: list[dict], cues: list[list], *, burned: bool = True, width: int = 1920, height: int = 1080
) -> dict:
    duration = max(scene["end"] for scene in scenes)
    return {
        "version": 1,
        "width": width,
        "height": height,
        "fps": 30,
        "duration": duration,
        "frames": int(duration * 30),
        "fade_in": 0.0,
        "fade_out": 0.0,
        "theme": build_theme(mode="dark", background_style="grid"),
        "captions": {"enabled": burned, "style": "pill", "position": "bottom", "cues": cues},
        "watermark": "none",
        "scenes": scenes,
    }


def _problems(audit: dict) -> list[dict]:
    return [finding for scene in audit["scenes"] for finding in scene["findings"]]


_LONG_CUE = "Every alert is enriched, routed and closed before your analysts even open their queue in the morning."


_FULL_STATEMENT = {
    "lines": ["Know your *adversaries*", "before they strike", "across every sector", "and every region"],
    "kicker": "Trusted worldwide",
    "figure": "6,000+",
    "secondary": ["organisations run OpenCTI", "from CERTs to Fortune 500 SOCs"],
    "chips": ["Open source", "STIX 2.1", "Enterprise", "SaaS", "On premise", "Air gapped"],
    "reveal": "lines",
    "show_logo": True,
    "backdrop": None,
}


def full_card_document(*, burned: bool = True, width: int = 1920, height: int = 1080) -> dict:
    """A centred statement whose text fills its box down to the bottom of the frame."""
    card = _scene("proof", "statement", 0, 8, align="center", **_FULL_STATEMENT)
    return _document([card], [[0.3, 7.9, _LONG_CUE]], burned=burned, width=width, height=height)


def lower_third_document() -> dict:
    """A short title and a lower third wide enough to sit under the centred subtitles."""
    card = _scene(
        "intro",
        "title",
        0,
        6,
        title="Meet the *team*",
        style="gradient",
        align="center",
        show_logo=False,
        overlays=[
            {
                "type": "lower_third",
                "text": "Samuel Hassine, Chief Executive Officer and co-founder",
                "subtext": "Filigran - the open source threat intelligence company",
                "t0": 1.0,
                "t1": 5.5,
            }
        ],
    )
    return _document([card], [[0.3, 5.9, _LONG_CUE]])


def badge_document() -> dict:
    statement = _scene(
        "proof",
        "statement",
        0,
        8,
        align="left",
        overlays=[{"type": "badge", "text": "Open source", "position": "top_left", "t0": 0.5, "t1": 7.5}],
        **_FULL_STATEMENT,
    )
    return _document([statement], [[0.3, 7.9, "Six thousand organisations already run OpenCTI."]])


def callouts_document() -> dict:
    recording = _scene(
        "demo",
        "recording",
        0,
        6,
        src="take/demo",
        src_fps=30,
        src_frames=180,
        src_w=1600,
        src_h=900,
        css_w=1600,
        css_h=900,
        time_map=[[0, 0], [6, 6]],
        camera=[[0, 0.5, 0.5, 1]],
        cursor={"visible": False, "size": 1, "style": "arrow", "click_effect": "none", "samples": [], "clicks": []},
        highlights=[],
        urls=[[0, "https://demo.example.com/playbooks"]],
        frame={"style": "window", "url_bar": True, "padding": 0.055, "radius": 18, "shadow": True},
        preview_label="Recording",
        overlays=[
            {
                "type": "callout",
                "text": "Drag a component in",
                "box": [0.1, 0.5, 0.2, 0.08],
                "style": "accent",
                "t0": 1.0,
                "t1": 5.0,
            },
            {
                "type": "callout",
                "text": "Then wire it to the next step",
                "box": [0.1, 0.5, 0.2, 0.08],
                "style": "glass",
                "t0": 2.0,
                "t1": 5.0,
            },
        ],
    )
    return _document([recording], [[0.3, 5.9, _LONG_CUE]])


def long_word_document() -> dict:
    card = _scene(
        "code",
        "title",
        0,
        5,
        title="https://filigran.io/opencti/playbooks/automation/enterprise/edition/configuration/connectors",
        style="gradient",
        align="left",
    )
    return _document([card], [])


async def test_a_full_card_stays_clear_of_two_lines_of_subtitles():
    audit = await _audit(full_card_document())
    assert _problems(audit) == []
    assert audit["captions"][0]["lines"] <= 2
    assert 0 < audit["scenes"][0]["fit"] <= 1


async def test_subtitles_left_to_the_player_keep_the_same_band_clear():
    """Not burned in, the WebVTT track is drawn by the player in the band every scene keeps clear."""
    assert _problems(await _audit(full_card_document(burned=False))) == []


async def test_a_vertical_video_keeps_its_text_clear_of_its_subtitles():
    assert _problems(await _audit(full_card_document(width=1080, height=1920))) == []


async def test_a_lower_third_stands_above_the_subtitles():
    assert _problems(await _audit(lower_third_document())) == []


async def test_a_badge_asked_over_the_text_takes_a_free_corner():
    assert _problems(await _audit(badge_document())) == []


async def test_two_callouts_on_one_element_never_cover_each_other():
    assert _problems(await _audit(callouts_document())) == []


async def test_a_word_too_long_for_the_frame_is_scaled_into_it():
    audit = await _audit(long_word_document())
    assert _problems(audit) == []
    assert audit["scenes"][0]["fit"] < 1


def _picture(**fields) -> dict:
    """An image overlay as the planner hands it to the page: sized in design pixels."""
    return {
        "type": "image",
        "text": "Brand mark",
        "src": "overlay/logo_mark",
        "position": "top_left",
        "w": 230.0,
        "h": 92.0,
        "opacity": 0.9,
        "t0": 0.5,
        "t1": 7.5,
        **fields,
    }


async def test_a_picture_asked_over_the_text_takes_a_free_corner_like_a_badge():
    statement = _scene("proof", "statement", 0, 8, align="left", overlays=[_picture()], **_FULL_STATEMENT)
    document = _document([statement], [[0.3, 7.9, "Six thousand organisations already run OpenCTI."]])
    assert _problems(await _audit(document)) == []


async def test_a_picture_too_big_for_any_free_spot_is_reported_as_its_own_group_even_faint():
    huge = _picture(w=1500.0, h=760.0, position="center", opacity=0.3)
    statement = _scene("proof", "statement", 0, 8, align="center", overlays=[huge], **_FULL_STATEMENT)
    document = _document([statement], [])
    overlaps = [f for f in _problems(await _audit(document)) if f["kind"] == "overlap"]
    assert overlaps and all("image overlay" in (f["block"], f["other"]) for f in overlaps)
    findings = quality.findings_from_audit({"scenes": [{"index": 0, "id": "proof", "findings": overlaps}]}, document, None)
    assert any('the image overlay "Brand mark"' in f.message for f in findings)


async def test_the_audit_reports_text_still_coming_in_when_its_scene_ends():
    rushed = _scene(
        "fast",
        "statement",
        0,
        1.2,
        lines=[
            "Four lines of headline type",
            "revealed one after the other",
            "in barely more than a second",
            "cannot be read",
        ],
        reveal="lines",
        align="center",
        show_logo=False,
        backdrop=None,
    )
    document = _document([rushed], [])
    audit = await _audit(document)
    assert "rushed" in {f["kind"] for f in _problems(audit)}
    findings = quality.findings_from_audit(audit, document, {"scenes": [{"id": "fast"}]})
    rushed_finding = next(f for f in findings if f.kind == "rushed")
    assert rushed_finding.scene_index == 0 and "make the scene at least" in rushed_finding.message


_VIDEO_TEMPLATES = [entry for entry in BUILTIN_TEMPLATE_CONTAINERS if entry["kind"] == "video"]


@pytest.mark.parametrize("entry", [None, *_VIDEO_TEMPLATES], ids=lambda e: e["slug"] if e else "freestyle")
async def test_a_video_just_created_reads_clean_on_every_built_in_template(entry):
    """`New video` saves one title card: the template's own cards around it must pass the check."""
    raw = {"title": "New video", "scenes": [{"type": "title", "title": "New video"}]}
    template = None
    if entry is None:
        raw["freestyle"] = True
    else:
        row = SimpleNamespace(
            id=uuid.uuid4(),
            name=entry["name"],
            kind="video",
            description=None,
            tags=entry["tags"],
            theme_config=entry["theme_config"],
            extra_metadata=entry["extra_metadata"],
        )
        template = templates.VideoTemplate.from_container(row)
    authored = authored_spec(raw)
    spec = parse_video_spec(templates.apply_template(authored, template).spec)
    theme = build_theme(
        mode=spec.theme.mode,
        background_style=spec.theme.background or "grid",
        template_palette=entry["theme_config"] if entry else None,
        has_logo=entry is not None,
        has_mark=entry is not None,
    )
    document = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme=theme).document
    findings = quality.findings_from_audit(await _audit(document), document, authored)
    assert [f.message for f in findings] == []
