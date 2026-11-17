"""Videos that are always good to watch: the quality check, animated stills, subtitles shown once.

* The quality check grades what the compositor's ``audit()`` measured (text
  covering text, text past the frame, text shrunk to fit, text still coming
  in) and what the timeline alone tells (reading time, subtitle speed, words
  over a bright backdrop, overlays too short to read) into findings that name
  the scene and the change; it never fails a save and rides the agent's tool
  result. The layout itself is proven in a real browser
  (``test_video_studio_layout_chromium.py``).
* An image scene with ``animate`` gets an AI shot made from it at save, plays
  it once made, the still until then - and never pays twice for the same one.
* Subtitles not burned in are a WebVTT track the player draws in the band the
  scenes keep clear (at the top when asked); burned in, the player leaves it off.
"""

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_PENDING, JOB_READY, VideoClip, VideoRender
from app.services.video_studio import clips, projects, quality, render, timeline
from app.services.video_studio.spec import authored_spec, parse_video_spec, referenced_ids, validate_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")

FILE_ID = "00000000-0000-0000-0000-0000000000f1"
OTHER_FILE = "00000000-0000-0000-0000-0000000000f2"
CLIP_ID = "00000000-0000-0000-0000-0000000000c1"


def _document(scenes: list[dict], cues: list[list] | None = None) -> dict:
    return {"width": 1920, "height": 1080, "scenes": scenes, "captions": {"enabled": True, "cues": cues or []}}


def _scene(sid: str, kind: str, start: float, end: float, **fields) -> dict:
    return {"id": sid, "type": kind, "start": start, "end": end, "overlays": [], **fields}


def _observed(index: int, start: float, end: float, *, fit: float = 1.0, reveal_end: float = 1.0, findings=()) -> dict:
    return {
        "index": index,
        "start": start,
        "end": end,
        "shown_until": end,
        "fit": fit,
        "reveal_end": reveal_end,
        "findings": list(findings),
    }


# ── Grading what the browser measured ─────────────────────────────────────


def test_text_under_the_subtitles_is_unreadable_and_the_remedy_is_named():
    document = _document([_scene("s1", "title", 0, 6, title="Automate your threat intelligence")])
    audit = {
        "scenes": [
            _observed(
                0,
                0,
                6,
                findings=[
                    {
                        "kind": "overlap",
                        "block": "subtitle",
                        "text": "Your analysts shouldn't",
                        "other": "subtitles",
                        "other_text": "Vos analystes",
                        "at": 2.4,
                    }
                ],
            )
        ]
    }
    [finding] = quality.findings_from_audit(audit, document, {"scenes": [{"id": "s1"}]})
    assert (finding.kind, finding.severity, finding.scene_index, finding.scene_id) == ("overlap", "overloaded", 0, "s1")
    assert 'the subtitle "Your analysts shouldn\'t" covers the subtitles "Vos analystes" at 2.4 s' in finding.message
    assert "fits above the subtitles" in finding.message and "captions.position" in finding.message


@pytest.mark.parametrize(
    ("block", "other", "words"),
    [
        ("badge", "kicker", "another position or time"),
        ("callout", "callout", "one after the other"),
        ("lower third", "subtitles", "while no one speaks"),
        ("watermark", "callout", "theme.watermark"),
    ],
)
def test_each_overlap_gets_the_remedy_of_what_collides(block, other, words):
    document = _document([_scene("s1", "recording", 0, 6)])
    finding = {"kind": "overlap", "block": block, "text": "a", "other": other, "other_text": "b", "at": 1}
    [graded] = quality.findings_from_audit({"scenes": [_observed(0, 0, 6, findings=[finding])]}, document, None)
    assert words in graded.message


def test_text_cut_by_the_frame_is_unreadable_and_text_touching_it_is_dense():
    document = _document([_scene("s1", "title", 0, 6)])
    cut = {"kind": "outside", "block": "title", "text": "https://...", "side": "right", "amount": 140}
    touching = {"kind": "outside", "block": "chip", "text": "STIX", "side": "left", "amount": -4}
    graded = quality.findings_from_audit({"scenes": [_observed(0, 0, 6, findings=[cut, touching])]}, document, None)
    assert [(f.severity, f.message) for f in graded] == [
        ("overloaded", 'the title "https://..." runs 140 px past the right edge of the frame - shorten it'),
        ("dense", 'the chip "STIX" touches the left edge of the frame - shorten it'),
    ]


def test_text_shrunk_to_fit_says_where_and_how_much():
    document = _document([_scene("s1", "statement", 0, 8, lines=["x"])], cues=[[1, 7, "Narration."]])
    [graded] = quality.findings_from_audit({"scenes": [_observed(0, 0, 8, fit=0.62)]}, document, None)
    assert (graded.kind, graded.severity) == ("fit", "overloaded")
    assert "scaled down to 62% to fit above the subtitles" in graded.message
    dense = quality.findings_from_audit({"scenes": [_observed(0, 0, 8, fit=0.8)]}, _document(document["scenes"]), None)
    assert dense[0].severity == "dense" and "to fit the frame" in dense[0].message
    assert quality.findings_from_audit({"scenes": [_observed(0, 0, 8, fit=0.9)]}, document, None) == []


def test_text_still_coming_in_asks_for_a_duration_and_is_not_also_told_to_read():
    scene = _scene(
        "s1",
        "statement",
        0,
        2,
        lines=["Four lines of headline", "type revealed", "one after the other", "cannot be read"],
    )
    rushed = {"kind": "rushed", "block": "line", "text": "cannot be read"}
    graded = quality.findings_from_audit(
        {"scenes": [_observed(0, 0, 2, reveal_end=2.8, findings=[rushed])]}, _document([scene]), None
    )
    assert [f.kind for f in graded] == ["rushed"]
    assert "make the scene at least" in graded[0].message


def test_words_on_screen_too_briefly_to_read_ask_for_a_duration():
    scene = _scene("s1", "title", 0, 3, title="Every alert enriched, routed and closed before your analysts open it")
    [graded] = quality.findings_from_audit({"scenes": [_observed(0, 0, 3, reveal_end=1.9)]}, _document([scene]), None)
    assert graded.kind == "reading" and graded.severity == "dense"
    assert "are all on screen for 1.1 s" in graded.message and "s long (duration)" in graded.message


def test_a_subtitle_on_three_lines_names_its_sentence():
    document = _document([_scene("s1", "recording", 0, 6)], cues=[[0.5, 5, "A very long sentence of narration."]])
    audit = {"scenes": [_observed(0, 0, 6)], "captions": [{"index": 0, "lines": 3, "font": 26}]}
    [graded] = quality.findings_from_audit(audit, document, None)
    assert graded.kind == "caption_lines" and "A very long sentence of narration." in graded.message


def test_a_template_scene_is_named_as_the_templates_and_the_worst_come_first():
    document = _document([_scene("intro", "title", 0, 3), _scene("s1", "title", 3, 9)])
    audit = {
        "scenes": [
            _observed(0, 0, 3, fit=0.8),
            _observed(
                1, 3, 9, findings=[{"kind": "outside", "block": "title", "text": "x", "side": "top", "amount": 20}]
            ),
        ]
    }
    graded = quality.findings_from_audit(audit, document, {"scenes": [{"id": "s1"}]})
    assert [(f.severity, f.scene_index) for f in graded] == [("overloaded", 0), ("dense", None)]
    assert "the template's title scene" in quality.format_quality_report(quality.QualityReport(2, tuple(graded)))


# ── What the timeline alone tells ─────────────────────────────────────────


def test_subtitles_too_fast_words_over_a_bright_backdrop_and_overlays_too_short():
    scenes = [
        _scene(
            "s1",
            "recording",
            0,
            4,
            overlays=[{"type": "callout", "text": "Drag every component of the pipeline in", "t0": 1, "t1": 1.6}],
        ),
        _scene("s2", "statement", 4, 10, lines=["x"], backdrop={"kind": "video", "dim": 0.1}),
    ]
    cues = [[0.2, 3.2, "x" * 100]]
    graded = {f.kind: f for f in quality.document_findings(_document(scenes, cues), None)}
    assert "33 characters a second" in graded["caption_speed"].message and graded["caption_speed"].scene_id == "s1"
    assert graded["backdrop"].severity == "advisory" and "darkened only to 0.10" in graded["backdrop"].message
    assert "shows for 0.6 s - too short to read" in graded["overlay_short"].message


# ── The words the agent reads, and when the check does not run ───────────


def test_the_report_reads_clean_skipped_switched_off_and_truncated():
    assert quality.format_quality_report(quality.QualityReport(0, skipped="disabled")) == ""
    assert (
        quality.format_quality_report(quality.QualityReport(0, skipped="no browser"))
        == "Quality check skipped: no browser."
    )
    assert "nothing covers anything" in quality.format_quality_report(quality.QualityReport(3))
    many = tuple(
        quality.QualityFinding(i, f"s{i}", "title", "", "fit", "dense", "too dense")
        for i in range(quality.MAX_REPORTED_FINDINGS + 2)
    )
    text = quality.format_quality_report(quality.QualityReport(10, many))
    assert text.startswith(f"Quality check: {len(many)} issue(s) the audience would see. Fix them and save again:")
    assert "- scene 0 [s0] title (dense): too dense" in text and text.endswith("the next check lists the rest.")
    # The cache round-trip keeps every field.
    assert quality.QualityReport.from_dict(
        quality.QualityReport(10, many, revision=4).as_dict()
    ) == quality.QualityReport(10, many, revision=4)


def test_a_switched_off_check_is_skipped(monkeypatch):
    monkeypatch.setattr(quality.settings, "video_quality_check_enabled", False)
    report = asyncio.run(quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=1))
    assert report.skipped == "disabled"


def test_a_check_that_cannot_plan_the_video_never_fails_the_save(monkeypatch):
    async def nothing_cached(_key):
        return None

    def broken_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(quality, "_cached_report", nothing_cached)
    monkeypatch.setattr("app.core.database.async_session", broken_session)
    report = asyncio.run(quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=1))
    assert report.skipped == "the video could not be planned for the check" and not report.findings


def test_a_report_of_the_same_revision_is_served_from_the_cache(monkeypatch):
    kept = quality.QualityReport(2, revision=3)

    async def cached(key):
        assert key.endswith(":3")
        return kept

    async def no_content(*_args, **_kwargs):
        return quality.ContentReport([])

    monkeypatch.setattr(quality, "_cached_report", cached)
    monkeypatch.setattr(quality, "check_content", no_content)
    assert asyncio.run(quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=3)) == kept


def test_the_editor_reads_the_check_and_the_agent_reads_it_after_every_save():
    from app.api.v1 import video_studio as video_studio_api
    from app.services.builtin_tools import video_studio as tool

    assert any(
        r.path == "/projects/{project_id}/quality" and "GET" in r.methods for r in video_studio_api.router.routes
    )
    assert tool.check_video_quality is quality.check_video_quality
    assert tool.format_quality_report is quality.format_quality_report


def test_the_editor_primes_the_assistant_in_the_words_of_the_agents_report():
    """`Fix with my assistant` writes the findings as the tool result does, within the same bound."""
    editor = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "video-studio" / "editor"
    strip = (editor / "QualityStrip.tsx").read_text(encoding="utf-8")
    assert f"const MAX_PRIMED_FINDINGS = {quality.MAX_REPORTED_FINDINGS};" in strip
    findings = (
        quality.QualityFinding(
            0,
            "broken",
            "clip",
            "Opening",
            "shot_state",
            "overloaded",
            'its AI shot "Opening drone shot" failed - generate it again in place',
        ),
        quality.QualityFinding(None, None, "outro", "Thank you", "overlap", "dense", "the watermark covers the logo"),
    )
    lines = quality.format_quality_report(quality.QualityReport(3, findings)).split("\n")[1:]
    expected = (editor / "QualityStrip.test.tsx").read_text(encoding="utf-8")
    assert lines and all(f"`{line}`" in expected for line in lines)


# ── Animated stills ───────────────────────────────────────────────────────


def test_animate_true_is_the_short_way_and_its_shot_is_a_reference():
    spec = {
        "title": "Film",
        "freestyle": True,
        "scenes": [{"id": "s1", "type": "image", "file_id": FILE_ID, "animate": True}],
    }
    authored = authored_spec(spec)
    assert authored["scenes"][0]["animate"] == {}
    authored["scenes"][0]["animate"]["clip_id"] = CLIP_ID
    assert CLIP_ID in referenced_ids(authored)["clip"]
    off = authored_spec({**spec, "scenes": [{"id": "s1", "type": "image", "file_id": FILE_ID, "animate": False}]})
    assert "animate" not in off["scenes"][0]


class _Rows:
    def __init__(self, rows=None):
        self.rows = rows or {}

    async def get(self, _model, key):
        return self.rows.get(str(key))


def _animated(**animate) -> dict:
    return {
        "title": "Film",
        "scenes": [{"id": "s1", "type": "image", "file_id": FILE_ID, "duration": 6, "animate": animate}],
    }


def test_an_animated_image_starts_its_shot_once_and_never_twice(monkeypatch):
    started: list[dict] = []

    async def start(_db, _user, **kwargs):
        started.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(CLIP_ID)), "Gemini"

    monkeypatch.setattr(clips, "start_clip", start)
    user = SimpleNamespace(id=uuid.uuid4())
    authored = _animated(prompt="slow push in")
    count, notes = asyncio.run(projects.start_animations(_Rows(), user, authored, {"format": {"aspect_ratio": "9:16"}}))
    assert (count, notes) == (1, [])
    assert authored["scenes"][0]["animate"]["clip_id"] == CLIP_ID
    assert started[0]["reference_file_id"] == FILE_ID and started[0]["prompt"] == "slow push in"
    assert started[0]["aspect_ratio"] == "9:16" and started[0]["duration_seconds"] == 6
    # The editor's draft predates the shot: the stored spec's is kept, nothing is started again.
    made = VideoClip(id=uuid.UUID(CLIP_ID), params={"reference_file_id": FILE_ID}, status=JOB_PENDING)
    draft = _animated()
    count, _ = asyncio.run(projects.start_animations(_Rows({CLIP_ID: made}), user, draft, {}, previous=authored))
    assert count == 0 and draft["scenes"][0]["animate"]["clip_id"] == CLIP_ID and len(started) == 1


def test_a_new_image_gets_a_new_shot_and_no_video_model_leaves_a_still(monkeypatch):
    calls: list[str] = []

    async def start(_db, _user, **kwargs):
        calls.append(kwargs["reference_file_id"])
        if kwargs["reference_file_id"] == OTHER_FILE:
            raise clips.ClipError("No video model is available: sora-2 was retired by its provider on 2026-09-24.")
        return SimpleNamespace(id=uuid.uuid4()), "Gemini"

    monkeypatch.setattr(clips, "start_clip", start)
    user = SimpleNamespace(id=uuid.uuid4())
    made_from_old = VideoClip(id=uuid.UUID(CLIP_ID), params={"reference_file_id": OTHER_FILE}, status=JOB_READY)
    authored = _animated(clip_id=CLIP_ID)
    count, _ = asyncio.run(projects.start_animations(_Rows({CLIP_ID: made_from_old}), user, authored, {}))
    assert count == 1 and calls == [FILE_ID] and authored["scenes"][0]["animate"]["clip_id"] != CLIP_ID
    still = {"title": "Film", "scenes": [{"id": "s2", "type": "image", "file_id": OTHER_FILE, "animate": {}}]}
    count, notes = asyncio.run(projects.start_animations(_Rows(), user, still, {}))
    assert count == 0 and "clip_id" not in still["scenes"][0]["animate"]
    assert notes == [
        "Scene s2 plays as a still: No video model is available: sora-2 was retired by its provider on 2026-09-24."
    ]


def test_an_animated_image_plays_its_shot_slowed_to_fill_the_scene():
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Film",
                "freestyle": True,
                "scenes": [{"id": "s1", "type": "image", "file_id": FILE_ID, "duration": 6, "animate": {}}],
            }
        )
    )
    shot = timeline.ClipMedia(duration=4.0, width=1280, height=720, fps=24.0, frame_count=96)
    payload = timeline.plan(spec, timeline.PlanMedia(clips={"s1": shot}), fps=30, theme={}).document["scenes"][0]
    assert (payload["type"], payload["src"]) == ("clip", "clip/s1")
    # The 4 s shot fills the 6 s scene as far as SLOWEST_FILL lets it, then holds its last frame.
    assert payload["time_map"] == [[0.0, 0.0], [5.7143, 4.0], [6.0, 4.0]]
    still = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={}).document["scenes"][0]
    assert (still["type"], still["src"]) == ("image", "image/s1")


def test_a_render_never_waits_for_an_animation(monkeypatch):
    from contextlib import asynccontextmanager

    pending = VideoClip(id=uuid.UUID(CLIP_ID), name="Animated image (s1)", status=JOB_PENDING, params={})

    @asynccontextmanager
    async def session():
        yield _Rows({CLIP_ID: pending})

    async def no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", no_groups)
    spec = parse_video_spec(
        validate_video_spec(
            {
                "title": "Film",
                "freestyle": True,
                "scenes": [{"id": "s1", "type": "image", "file_id": FILE_ID, "animate": {"clip_id": CLIP_ID}}],
            }
        )
    )
    found = asyncio.run(render._read_references(spec, SimpleNamespace(id=uuid.uuid4(), role="user"), None))
    assert found["animations"] == {}
    assert (
        "still being generated" in found["animation_notes"][0]
        and "plays as the still image" in found["animation_notes"][0]
    )


def test_the_agent_and_the_skill_know_how_to_animate_a_still():
    from app.services.builtin_tools.video_studio import _definitions
    from tests.test_video_studio_mixed import SKILL_PATH

    contract = next(d for d in _definitions(["high"]) if d.name == "create_video").description
    assert "animate? (true, or {prompt: how it moves}" in contract
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "animate: true" in skill and "`ai_shots`" in skill


def test_a_shot_whose_model_retired_is_refused_and_made_again_only_on_a_model_named(monkeypatch):
    from app.services.video_studio import jobs

    asked: list[tuple] = []
    live = SimpleNamespace(id=uuid.uuid4(), provider="gemini", display_name="Gemini")

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        asked.append((model, provider_id))
        if model == "sora-2-pro":
            raise clips.ClipError("sora-2-pro was retired by its provider on 2026-09-24: pick another video model.")
        return object(), "veo-3.1-generate-preview", live

    async def enqueue(*_args, **_kwargs):
        return None

    class _Session:
        async def refresh(self, _row, *, with_for_update=False):
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(clips, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    shot = VideoClip(
        id=uuid.UUID(CLIP_ID),
        provider="openai",
        model="sora-2-pro",
        prompt="Particles over a globe",
        params={"provider_id": str(uuid.uuid4()), "duration_seconds": 8},
        status="failed",
        error="no video generation",
    )
    # One model per round: its retirement is said and the model to make it with asked, never the platform's
    # live one taken in its place.
    with pytest.raises(clips.ClipError, match=r"^The shot's model sora-2-pro can no longer make it \(.*retired.*\): name the model") as refused:
        asyncio.run(clips.restart_clip(_Session(), SimpleNamespace(id=uuid.uuid4()), shot))
    assert refused.value.about_model and shot.model == "sora-2-pro" and (None, None) not in asked
    # A model the person names is theirs to change: its retirement is said, not replaced.
    shot.status = "failed"
    with pytest.raises(clips.ClipError, match="retired"):
        asyncio.run(clips.restart_clip(_Session(), SimpleNamespace(id=uuid.uuid4()), shot, model="sora-2-pro"))
    # Named, a live model makes it.
    shot.status = "failed"
    assert asyncio.run(clips.restart_clip(_Session(), SimpleNamespace(id=uuid.uuid4()), shot, model="veo-3.1-generate-preview")).label == "Gemini"
    assert shot.model == "veo-3.1-generate-preview"


def test_the_watermark_waits_for_the_content_and_leaves_the_cards_to_the_brand():
    from app.services.video_studio import compositor_page

    js = compositor_page.PAGE_JS
    # A motion scene drawing a brand mark carries the brand like a card does.
    assert (
        "const brandsItself = (sc) => sc.type === 'title' || sc.type === 'outro' || (sc.type === 'statement' && !!sc.show_logo)\n"
        "  || (sc.type === 'motion' && !!sc.brands);"
        in js
    )
    assert "wm.style.opacity = String(0.88 * watermarkAlpha(T));" in js


def test_the_brand_films_open_on_a_centred_card():
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    for slug in ("filigran-brand-film", "xtm-one-brand-film"):
        video = next(t for t in BUILTIN_TEMPLATE_CONTAINERS if t["slug"] == slug)["extra_metadata"]["video"]
        assert video["intro"]["required"] is True, slug
        assert video["scene_defaults"]["title"]["align"] == "center", slug


# ── Subtitles shown once, where the scenes keep room for them ────────────


def test_the_webvtt_track_is_drawn_where_the_subtitles_are_placed():
    cues = [[0.0, 1.5, "Hello."]]
    assert "00:00:00.000 --> 00:00:01.500\n" in timeline.vtt_from_cues(cues)
    assert "00:00:00.000 --> 00:00:01.500 line:0\n" in timeline.vtt_from_cues(cues, "top")


def test_a_player_leaves_burned_in_subtitles_off():
    from app.services.video_studio.projects import serialize_render

    base = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "revision": 1,
        "quality": "standard",
        "status": JOB_READY,
        "progress": 100,
    }
    assert serialize_render(VideoRender(**base, captions_burned_in=None))["captions_burned_in"] is True
    assert serialize_render(VideoRender(**base, captions_burned_in=False))["captions_burned_in"] is False
