"""Figure scenes: one block of a deck drawn on the video's palette and built in.

* the scene: a deck address or a block written in it, validated by the
  deck's own validator; exactly one source; its files and its deck are
  references; a save names an address that names no block;
* the source: the block a deck address names (a dashboard's panels and a
  columns slide counted in order, a roadmap slide's phases), the one-slide
  deck the figure page draws, on the template's palette and without chrome;
* the timing: steps spread over the scene, together, or when the narration
  names them - however it writes their labels, in order, the rest spread to
  the scene's end; the frames drawn only where something moves;
* the render: a figure drawn for its planned length and narration, its page
  served by the render process, then played as a shot.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import figures, projects, render, speech_timing, timeline
from app.services.video_studio import spec as spec_module
from app.services.video_studio.spec import (
    VideoSpecError,
    figure_block_problems,
    figure_file_ids,
    parse_video_spec,
    referenced_ids,
    spec_summary,
    validate_video_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
DECK_ID = "00000000-0000-0000-0000-00000000f001"
PICTURE_ID = "00000000-0000-0000-0000-00000000f002"
FLOW = {"type": "process_flow", "steps": [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Act"}]}


def _figure(**fields) -> object:
    raw = {"title": "Explainer", "scenes": [{"id": "fig", "type": "figure", **fields}]}
    return parse_video_spec(validate_video_spec(raw)).scenes[0]


# ── The scene ─────────────────────────────────────────────────────────────


def test_a_figure_is_a_block_written_in_the_scene_or_a_decks_block():
    written = _figure(block_spec=FLOW)
    assert written.block_spec == FLOW and written.presentation_id is None and written.slide is None
    assert (written.duration, written.build, written.reveal, written.camera) == (6.0, "sequence", "even", "follow")
    assert written.dwell == spec_module.DEFAULT_FIGURE_DWELL == 0.8 and _figure(block_spec=FLOW, dwell=2).dwell == 2.0
    with pytest.raises(VideoSpecError, match="dwell"):
        _figure(block_spec=FLOW, dwell=9)
    # Built, it keeps moving unless asked to hold.
    assert written.motion == "drift" and _figure(block_spec=FLOW, motion="none").motion == "none"
    with pytest.raises(VideoSpecError, match="motion"):
        _figure(block_spec=FLOW, motion="spin")
    from_deck = _figure(presentation_id=DECK_ID, block=2)
    assert (from_deck.presentation_id, from_deck.slide, from_deck.block) == (DECK_ID, 1, 2)
    assert _figure(presentation_id=DECK_ID, slide_id="slide-arch").slide is None


def test_a_figure_has_exactly_one_source():
    with pytest.raises(VideoSpecError, match="exactly one of presentation_id"):
        _figure(presentation_id=DECK_ID, block_spec=FLOW)
    with pytest.raises(VideoSpecError, match="exactly one of presentation_id"):
        _figure(duration=4)


def test_a_written_block_is_checked_by_the_decks_own_validator():
    assert figure_block_problems(FLOW) == []
    one_step = {"type": "process_flow", "steps": [{"title": "Alone"}]}
    problems = figure_block_problems(one_step)
    assert problems and all(problem.startswith("block_spec") for problem in problems)
    with pytest.raises(VideoSpecError, match="block_spec"):
        _figure(block_spec=one_step)
    assert figure_block_problems({"steps": []}) == [
        "a figure's block_spec is a block of a presentation spec, with its `type`"
    ]
    assert figure_block_problems({"type": "no_such_block"})


def test_a_figures_deck_and_pictures_are_references_checked_like_any():
    nested = {
        "type": "architecture",
        "zones": [{"nodes": [{"logo_file_id": PICTURE_ID, "label": "x"}]}],
        "file_id": "not-a-uuid",
    }
    assert figure_file_ids(nested) == {PICTURE_ID}
    refs = referenced_ids(
        {
            "scenes": [
                {"type": "figure", "presentation_id": DECK_ID},
                {"type": "figure", "block_spec": {"type": "image", "file_id": PICTURE_ID}},
            ]
        }
    )
    assert refs["presentation"] == {DECK_ID} and refs["file"] == {PICTURE_ID}


def test_a_figure_is_summed_up_by_where_its_block_comes_from():
    summary = spec_summary(
        {
            "scenes": [
                {"type": "figure", "presentation_id": DECK_ID, "slide": 4, "block": 1},
                {"type": "figure", "block_spec": FLOW},
            ]
        }
    )
    assert f"block 1 of deck {DECK_ID} slide 4" in summary and "process_flow written in the scene" in summary


def test_writing_a_block_drops_the_deck_address_it_replaces():
    spec = {
        "title": "x",
        "scenes": [{"id": "fig", "type": "figure", "presentation_id": DECK_ID, "slide": 3, "block": 1}],
    }
    edited, _ = projects.apply_operations(
        spec, [{"op": "update_scene", "scene": "fig", "fields": {"block_spec": FLOW}}]
    )
    scene = edited["scenes"][0]
    assert "presentation_id" not in scene and scene["block_spec"] == FLOW
    validate_video_spec(edited)


def test_a_save_names_a_deck_address_that_names_no_block():
    deck = {
        "slides": [
            {"layout": "content", "blocks": [FLOW]},
            {"layout": "title", "title": "Cover"},
        ]
    }

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: deck)

    def problems(**scene):
        spec = {"scenes": [{"type": "figure", "presentation_id": DECK_ID, **scene}]}
        found = asyncio.run(
            projects._figure_address_problems(_Db(), spec, SimpleNamespace(id=uuid.uuid4()), [], is_admin=False)
        )
        return [problem.message for problem in found]

    assert problems(slide=1, block=0) == []
    assert "has 1 block(s); block 3 does not exist" in problems(slide=1, block=3)[0]
    assert "a title slide" in problems(slide=2)[0]
    assert "no slide 9" in problems(slide=9)[0]


# ── The source and the page's document ────────────────────────────────────


def _address(**fields) -> SimpleNamespace:
    return SimpleNamespace(**{"id": "fig", "slide": 1, "slide_id": None, "block": 0, **fields})


def test_a_deck_address_names_a_block_in_the_order_the_slide_draws_them():
    chart = {"type": "chart", "chart_type": "bar", "data": []}
    deck = {
        "slides": [
            {"layout": "dashboard", "panels": [{"block": FLOW}, {"title": "Trend", "block": chart}]},
            {"id": "s-cols", "layout": "content", "columns": [[FLOW], [chart]]},
            {
                "layout": "roadmap",
                "title": "Plan",
                "phases": [{"title": "Now", "items": [{"file_id": PICTURE_ID}]}],
                "footer_block": FLOW,
            },
        ]
    }
    deck_id = uuid.UUID(DECK_ID)
    panel = figures.source_from_deck(_address(block=1), deck, deck_id)
    assert panel.kind == "chart" and panel.slide == {"layout": "content", "blocks": [chart], "hide_footer": True}
    column = figures.source_from_deck(_address(slide=None, slide_id="s-cols", block=1), deck, deck_id)
    assert column.kind == "chart" and column.presentation_id == deck_id
    roadmap = figures.source_from_deck(_address(slide=3), deck, deck_id)
    assert (
        roadmap.kind == "roadmap" and "footer_block" not in roadmap.slide and roadmap.files == frozenset({PICTURE_ID})
    )


def test_a_written_block_is_its_own_source():
    source = figures.source_from_spec(_figure(block_spec={"type": "image", "file_id": PICTURE_ID, "alt": "Logo"}))
    assert source.kind == "image" and source.presentation_id is None and source.files == frozenset({PICTURE_ID})


def test_the_figure_page_reads_a_one_slide_deck_on_the_templates_palette_without_chrome():
    palette = {"background": "#0b0e14", "accent": "#00bcd4"}
    document = figures.figure_document(
        figures.source_from_spec(_figure(block_spec=FLOW)), scheme="dark", palette=palette
    )
    deck = document["presentation"]
    assert document["kind"] == "process_flow" and deck["slide_count"] == 1 and deck["theme"] == "dark"
    assert deck["theme_config"] == palette and deck["theme_configs"] == {"dark": palette}
    chrome = deck["spec"]["chrome"]
    assert (chrome["show_logo"], chrome["show_footer"], chrome["show_slide_numbers"]) == (False, False, False)
    assert deck["spec"]["slides"][0]["blocks"][0]["type"] == "process_flow"
    broken = figures.FigureSource(
        "process_flow", {"layout": "content", "blocks": [{"type": "process_flow", "steps": []}]}, None, frozenset()
    )
    with pytest.raises(figures.FigureError, match="not a block a deck can draw"):
        figures.figure_document(broken, scheme="dark", palette=palette)


# ── When each step comes in, and which frames are drawn ───────────────────


def test_step_k_of_n_comes_in_at_k_equal_shares_of_the_scene_before_its_whole_figure_hold():
    # A zone and six nodes, 16 s.
    labels = [[f"step {k}"] for k in range(7)]
    times = figures.reveal_times(labels, length=16.0)
    slot = (16.0 - figures.WHOLE_HOLD - figures.FIRST_STEP_AT) / 7
    assert times == [round(figures.FIRST_STEP_AT + k * slot, 4) for k in range(7)]
    # A quarter, a half and three quarters into the scene: about a quarter, a half, three quarters of the steps.
    for share in (0.25, 0.5, 0.75):
        assert abs(sum(t <= 16.0 * share for t in times) - 7 * share) <= 1
    # Every step is in, and has dwelt, before the scene ends on the whole figure.
    assert times[-1] + figures.DEFAULT_SETTLE + spec_module.DEFAULT_FIGURE_DWELL <= 16.0 - figures.WHOLE_HOLD + 1e-6
    assert figures.reveal_times([["a"], ["b"]], length=8.0, together=True) == [0.35, 0.5]
    assert figures.reveal_times([], length=8.0) == []


def test_steps_with_no_room_to_dwell_come_in_together_a_few_at_a_time_never_all_at_the_end():
    labels = [[f"node {k}"] for k in range(12)]
    times = figures.reveal_times(labels, length=8.0, dwell=0.8, settle=[0.95] * 12)
    beats = sorted(set(times))
    # Each beat stays on screen its settle and dwell before the next one starts...
    assert all(b - a >= 0.95 + 0.8 - 1e-6 for a, b in itertools.pairwise(beats))
    # ...the beats are spread over the scene, a few steps each, in order.
    assert 3 <= len(beats) < 12 and times == sorted(times)
    assert max(times.count(beat) for beat in beats) <= 4 and beats[-1] < 8.0 - figures.WHOLE_HOLD
    # With no dwell asked, every step has its own time.
    assert len(set(figures.reveal_times(labels, length=8.0, dwell=0.0, settle=[0.0] * 12))) == 12


def _said(text: str, start: float = 1.0, seconds: float = 6.0):
    return speech_timing.paced_words(text, start, seconds)


def test_a_step_comes_in_when_the_narration_says_its_label():
    words = _said("First we collect the signal, then we enrich it, and finally we act on what matters.")
    at = {word.text: word.start for word in words}
    labels = [["Collect"], ["Enrich"], ["Act on what matters"]]
    times = figures.reveal_times(labels, length=16.0, mode="narration", words=words, dwell=0.0)
    assert times == [pytest.approx(at[w] - figures.NARRATION_LEAD, abs=1e-3) for w in ("collect", "enrich", "act")]
    # A label is found whole ("XTM One") before any of its words alone, and in the order of the steps.
    words = _said("One agent: XTM One. Then OpenCTI.")
    assert [word.text for word in words[:4]] == ["one", "agent", "xtm", "one"]
    times = figures.reveal_times([["XTM One"], ["OpenCTI"]], length=16.0, mode="narration", words=words, dwell=0.0)
    assert times[0] == pytest.approx(words[2].start - figures.NARRATION_LEAD, abs=1e-3)
    assert times[1] == pytest.approx(words[5].start - figures.NARRATION_LEAD, abs=1e-3)
    # A narration naming none of them: spread as if it said nothing.
    assert figures.reveal_times(
        labels, length=10.0, mode="narration", words=_said("Nothing here.")
    ) == figures.reveal_times(labels, length=10.0)


def test_the_steps_a_narration_does_not_name_spread_evenly_around_those_it_does():
    words = _said("We collect, we enrich.", start=1.0, seconds=3.0)
    labels = [["Zone"], ["Collect"], ["Unnamed"], ["Enrich"], ["Later"], ["Last"]]
    times = figures.reveal_times(labels, length=16.0, mode="narration", words=words, dwell=0.0, settle=[0.0] * 6)
    at = {word.text: word.start - figures.NARRATION_LEAD for word in words}
    assert times[0] == figures.FIRST_STEP_AT and times[1] == pytest.approx(at["collect"], abs=1e-3)
    assert times[2] == pytest.approx((at["collect"] + at["enrich"]) / 2, abs=1e-3)
    # After the last named step, the rest spread evenly up to the whole figure's hold - never piled at the end.
    end = 16.0 - figures.WHOLE_HOLD
    share = (end - times[3]) / 3
    assert times[4] == pytest.approx(times[3] + share, abs=1e-3) and times[5] == pytest.approx(
        times[3] + 2 * share, abs=1e-3
    )


def _named(narration: str, labels: list[list[str]], length: float = 20.0) -> list[figures.Reveal]:
    return figures.reveals(
        labels,
        length=length,
        mode="narration",
        words=_said(narration, start=0.5, seconds=12.0),
        dwell=0.0,
        settle=[0.0] * len(labels),
    )


def test_a_label_is_found_however_the_narration_writes_it():
    """Case, punctuation, a possessive, a plural, a compound in one word or two, a part of a label, a near spelling."""
    labels = [
        ["XTM One"],
        ["OpenCTI - Threat intelligence"],
        ["Open AEV"],
        ["Connector"],
        ["Risk quantification (OpenCRQ)"],
        ["Compliance"],
    ]
    narration = (
        "It all runs on xtm-one. OpenCTI's knowledge comes first, then OpenAEV tests it; "
        "the connectors feed both, OpenCRQ prices the exposure and the complience team signs off."
    )
    found = _named(narration, labels)
    said = [reveal.said for reveal in found]
    assert said == ["xtm-one", "opencti's", "openaev", "connectors", "opencrq", "complience"]
    # Each step at the word that names it, in order.
    words = _said(narration, start=0.5, seconds=12.0)
    starts = {word.text: word.start for word in words}
    assert [reveal.at for reveal in found] == [
        pytest.approx(max(figures.FIRST_STEP_AT, starts[s] - figures.NARRATION_LEAD), abs=1e-3) for s in said
    ]


def test_a_word_every_step_shares_names_none_of_them_alone():
    # "Service" is in every label: only the words that tell the steps apart name them - never "a service" first.
    labels = [["Ingest service"], ["Enrichment service"], ["Alerting service"]]
    found = _named("A service ingests the feed, enrichment adds context, then alerting pages the analyst.", labels)
    assert [reveal.said for reveal in found] == ["ingests", "enrichment", "alerting"]
    found = _named("Every service matters: enrichment adds context, then alerting pages the analyst.", labels)
    assert [reveal.said for reveal in found] == [None, "enrichment", "alerting"]


def test_one_step_said_late_never_keeps_the_steps_after_it_from_being_named():
    # The first step's only cue comes in the closing sentence: matched in order, it would hide every other step.
    labels = [["Platform"], ["Collect"], ["Enrich"], ["Respond"], ["Report"]]
    narration = "We collect the signal, enrich it, respond to the threat and report on it, all on one platform."
    found = _named(narration, labels, length=30.0)
    assert [reveal.said for reveal in found] == [None, "collect", "enrich", "respond", "report"]
    assert [reveal.at for reveal in found] == sorted(reveal.at for reveal in found)


def test_the_steps_after_the_last_one_named_spread_to_the_scene_end_never_stacked():
    labels = [["Collect"], ["Enrich"], *[[f"Phase {k}"] for k in range(4)]]
    found = _named("We collect the signal, then we enrich it.", labels, length=20.0)
    times = [reveal.at for reveal in found]
    end = 20.0 - figures.WHOLE_HOLD
    assert [reveal.said for reveal in found[:2]] == ["collect", "enrich"] and all(r.said is None for r in found[2:])
    # The four unnamed steps share what is left after "enrich" evenly, up to the whole figure's hold.
    gaps = [b - a for a, b in itertools.pairwise([*times[1:], end])]
    assert gaps == [pytest.approx((end - times[1]) / 5, abs=1e-3)] * 5 and gaps[0] > 1.0
    assert len(set(times)) == len(times) and times[-1] < end


def test_a_step_named_too_late_for_the_steps_after_it_waits_for_no_one():
    # The first step is named at the very end of a long narration: honoured, the five after it would pile up.
    words = _said(" ".join(["filler"] * 40) + " Collect.", start=0.5, seconds=13.0)
    labels = [["Collect"], *[[f"step {k}"] for k in range(5)]]
    times = figures.reveal_times(labels, length=16.0, mode="narration", words=words, dwell=0.8, settle=[0.95] * 6)
    assert times == figures.reveal_times(labels, length=16.0, dwell=0.8, settle=[0.95] * 6)
    assert len(set(times)) == 6


def test_each_word_is_shown_from_the_start_of_the_subtitle_that_holds_it():
    """A cue shows its whole phrase from its start, whatever the captions write otherwise."""
    words = _said("We collect the signal. Then we enrich it.", start=0.5, seconds=6.0)
    captions = [(0.5, 3.0, "We collect the signal."), (3.0, 6.5, "Then we enrich it.")]
    assert figures.shown_from(words, captions) == [0.5] * 4 + [3.0] * 4
    # A lexicon entry applied to the captions writes a word otherwise: it keeps the cue of the words around it.
    respelled = [(0.5, 3.0, "We kollekt the signal."), (3.0, 6.5, "Then we enrich it.")]
    assert figures.shown_from(words, respelled) == [0.5] * 4 + [3.0] * 4
    assert figures.shown_from(words, []) == [None] * len(words)


def test_a_step_named_in_a_subtitle_comes_in_with_it_not_when_the_voice_reaches_its_word():
    words = _said("We collect the signal. Then we enrich it.", start=0.5, seconds=6.0)
    at = {word.text: word.start for word in words}
    captions = [(0.5, 3.0, "We collect the signal."), (3.0, 6.5, "Then we enrich it.")]
    labels = [["Collect"], ["Enrich"]]
    shown = figures.reveals(labels, length=16.0, mode="narration", words=words, dwell=0.0, captions=captions)
    assert at["enrich"] > 3.0 + figures.NARRATION_LEAD
    assert [reveal.at for reveal in shown] == [pytest.approx(0.35), pytest.approx(3.0 - figures.NARRATION_LEAD)]
    assert [(reveal.said, reveal.subtitled) for reveal in shown] == [("collect", True), ("enrich", True)]
    # Without subtitles, the voice decides, as before.
    heard = figures.reveals(labels, length=16.0, mode="narration", words=words, dwell=0.0)
    assert [reveal.at for reveal in heard] == [
        pytest.approx(at[w] - figures.NARRATION_LEAD, abs=1e-3) for w in ("collect", "enrich")
    ]
    assert not any(reveal.subtitled for reveal in heard)
    # A voice that says the word before its subtitle shows it still leads.
    late = [(0.5, 3.0, "We collect the signal."), (at["enrich"] + 0.4, 6.5, "Then we enrich it.")]
    early = figures.reveals(labels, length=16.0, mode="narration", words=words, dwell=0.0, captions=late)
    assert early[1].at == pytest.approx(at["enrich"] - figures.NARRATION_LEAD, abs=1e-3) and not early[1].subtitled
    described = {"steps": [{"labels": ["Collect"]}, {"labels": ["Enrich"]}]}
    scene = _figure(block_spec=FLOW, reveal="narration", narration="We collect the signal. Then we enrich it.")
    line = figures.reveal_line(scene, described, shown, length=16.0, paced=False)
    assert '"Enrich" 2.85 s (said "enrich", with its subtitle)' in line


def test_only_the_frames_where_something_moves_are_drawn():
    frames = figures.frame_times([[0.0, 0.5], [2.0, 2.2]], length=4.0, fps=10)
    # Moving, then the first still frame after it; a still stretch is that one frame, held.
    assert frames[:7] == [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert 1.5 not in frames and 1.9 in frames and 2.3 in frames and 3.9 not in frames
    assert figures.frame_times([], length=1.0, fps=10) == [0.0]


# ── The timeline and the render ───────────────────────────────────────────


def _planned(media: timeline.PlanMedia) -> timeline.TimelinePlan:
    spec = parse_video_spec(
        validate_video_spec(
            {"title": "x", "scenes": [{"id": "fig", "type": "figure", "block_spec": FLOW, "duration": 5}]}
        )
    )
    return timeline.plan(spec, media, fps=30, theme={})


def test_an_undrawn_figure_is_planned_as_a_still_and_a_drawn_one_plays_like_a_shot():
    still = _planned(timeline.PlanMedia()).document["scenes"][0]
    assert (still["type"], still["src"], still["end"] - still["start"]) == ("image", "image/fig", 5.0)
    drawn = timeline.ClipMedia(duration=5.0, width=3840, height=2160, fps=30.0, frame_count=150)
    shot = _planned(timeline.PlanMedia(clips={"fig": drawn})).document["scenes"][0]
    assert (shot["type"], shot["src"], shot["fit"], shot["src_frames"]) == ("clip", "clip/fig", "contain", 150)
    assert shot["time_map"][0] == [0.0, 0.0] and shot["end"] - shot["start"] == 5.0


def test_the_render_draws_a_figure_for_its_planned_length_and_narration(monkeypatch, tmp_path):
    narration = "We collect, we enrich and we act on every signal the platform sees."
    raw = {
        "title": "x",
        "format": {"aspect_ratio": "16:9"},
        "scenes": [
            {"id": "open", "type": "title", "title": "Filigran", "duration": 3},
            {
                "id": "fig",
                "type": "figure",
                "block_spec": FLOW,
                "duration": 4,
                "reveal": "narration",
                "narration": narration,
            },
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = render._Media()
    media.figures["fig"] = (figures.source_from_spec(spec.scenes[1]), {"kind": "process_flow"})
    plan = timeline.plan(spec, media.plan, fps=30, theme={})
    drawn: dict = {}

    async def draw(source, document, **kw):
        drawn.update(kw, source=source)
        return str(tmp_path / "figure.mp4"), timeline.ClipMedia(
            duration=kw["length"], width=1920, height=1080, fps=30.0, frame_count=1
        )

    async def progress(*_a, **_kw):
        return None

    monkeypatch.setattr(figures, "draw", draw)
    asyncio.run(
        render._draw_figures(spec, plan, media, user=None, fps=30, scale=2, workdir=tmp_path, progress=progress)
    )
    placed = next(scene for scene in plan.document["scenes"] if scene["id"] == "fig")
    said_from, said_to = plan.narration_spans["fig"]
    assert drawn["length"] == pytest.approx(placed["end"] - placed["start"])
    # No voice spoken: its words paced over the span the plan gives the narration, on the scene's clock.
    words = drawn["words"]
    assert [word.text for word in words] == speech_timing.tokens(narration)
    span = (said_from - placed["start"], said_to - placed["start"])
    assert words[0].start == pytest.approx(span[0]) and span[1] - 0.5 < words[-1].end <= span[1]
    # Captions on (the default) and a narration: the figure keeps clear of their band.
    assert drawn["safe"] == figures.caption_safe_area(spec, spec.scenes[1], 1080) and drawn["safe"]["bottom"] > 0
    assert (drawn["fps"], drawn["scale"], drawn["viewport"]) == (30, 2, (1920, 1080))
    assert "fig" in media.plan.clips and "clip/fig" in media.assets.sources
    # Planned again with the drawing, the timeline is the same.
    again = timeline.plan(spec, media.plan, fps=30, theme={})
    assert (
        again.duration == plan.duration
        and next(s for s in again.document["scenes"] if s["id"] == "fig")["type"] == "clip"
    )


def _voice(path: Path, runs: list[tuple[float, float]], seconds: float, rate: int = 16000) -> str:
    """A synthesised voice: noise where someone speaks (*runs*, seconds), silence between - what ``speech_timing`` reads."""
    import wave

    import numpy as np

    noise = np.random.default_rng(6193)
    samples = np.zeros(round(seconds * rate), dtype=np.float32)
    for start, end in runs:
        a, b = round(start * rate), round(end * rate)
        samples[a:b] = noise.uniform(-0.5, 0.5, b - a)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((samples * 32767.0).astype("<i2").tobytes())
    return str(path)


def test_each_narrated_step_is_on_screen_by_the_first_frame_of_the_subtitle_naming_it(monkeypatch, tmp_path):
    """Three steps named in three sentences, the words measured on the voice: the picture never lags its caption."""
    narration = (
        "First we collect every signal from the sensors. Then the platform will enrich each alert with context. "
        "Finally the analyst will respond to the threat."
    )
    steps = [{"title": "Collect"}, {"title": "Enrich"}, {"title": "Respond"}]
    raw = {
        "title": "x",
        "format": {"aspect_ratio": "16:9"},
        "scenes": [
            {"id": "open", "type": "title", "title": "Filigran", "duration": 3},
            {
                "id": "fig",
                "type": "figure",
                "block_spec": {"type": "process_flow", "steps": steps},
                "duration": 4,
                "reveal": "narration",
                "narration": narration,
            },
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    media = render._Media()
    media.figures["fig"] = (figures.source_from_spec(spec.scenes[1]), {"kind": "process_flow"})
    # Each sentence its own run of speech, a pause between: the sentence's label is said well after it starts.
    media.voice_paths["fig"] = _voice(tmp_path / "voice.wav", [(0.0, 2.4), (2.9, 5.6), (6.1, 8.6)], 8.8)
    media.plan.voices["fig"] = timeline.VoiceMedia(duration=8.8)
    fps = 30
    plan = timeline.plan(spec, media.plan, fps=fps, theme={})
    drawn: dict = {}

    async def draw(source, document, **kw):
        drawn.update(kw)
        return str(tmp_path / "figure.mp4"), timeline.ClipMedia(
            duration=kw["length"], width=1920, height=1080, fps=30.0, frame_count=1
        )

    async def progress(*_a, **_kw):
        return None

    monkeypatch.setattr(figures, "draw", draw)
    asyncio.run(
        render._draw_figures(spec, plan, media, user=None, fps=fps, scale=2, workdir=tmp_path, progress=progress)
    )
    placed = next(scene for scene in plan.document["scenes"] if scene["id"] == "fig")
    described = {"steps": [{"labels": [step["title"]]} for step in steps]}
    shown = figures._reveals_of(
        described, scene=spec.scenes[1], length=drawn["length"], words=drawn["words"], captions=drawn["captions"]
    )
    assert [reveal.said for reveal in shown] == ["collect", "enrich", "respond"]
    cues = plan.document["captions"]["cues"]
    assert len(cues) == 3 and plan.captions["fig"] == cues
    heard = {word.text: word.start for word in drawn["words"]}

    def first_frame(seconds: float) -> int:
        return math.ceil(seconds * fps - 1e-6)

    for (cue_start, _cue_end, phrase), reveal, label in zip(cues, shown, ("collect", "enrich", "respond"), strict=True):
        assert label in phrase.lower()
        assert first_frame(placed["start"] + reveal.at) <= first_frame(cue_start), (label, reveal.at, cue_start)
    # The voice reaches "enrich" and "respond" more than a second into their sentences: their subtitles lead.
    assert heard["enrich"] + placed["start"] - cues[1][0] > 1.0 and shown[1].subtitled and shown[2].subtitled


def test_the_figure_page_is_served_by_the_render_and_screenshot_where_it_moves(monkeypatch, tmp_path):
    from app.services import presentation_export
    from app.services.video_studio import encoder

    source = figures.source_from_spec(_figure(block_spec={"type": "image", "file_id": PICTURE_ID, "alt": "Logo"}))
    document = {"kind": "image", "presentation": {"spec": {}}}
    opened: dict = {}
    evaluated: list = []

    class _Route:
        def __init__(self, url: str):
            self.request = SimpleNamespace(url=url)
            self.answered: dict = {}

        async def fulfill(self, **kw):
            self.answered = kw

    class _Page:
        async def evaluate(self, script, arg=None):
            evaluated.append((script, arg))
            if "describe" in script:
                return {"steps": [{"labels": ["Logo"]}, {"labels": ["Caption"]}]}
            if "plan" in script:
                return {"active": [[0.3, 1.2]]}
            return None

        async def screenshot(self, *, path, **_kw):
            Path(path).write_bytes(b"jpeg")

    @asynccontextmanager
    async def open_page(url, **kw):
        opened.update(kw, url=url)
        yield _Page()

    async def picture(_source, _user, file_id):
        return (b"png", "image/png") if file_id == PICTURE_ID else None

    encoded: dict = {}

    async def frames_to_mezzanine(frames, **kw):
        encoded.update(kw, frames=frames)

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(figures, "_picture", picture)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", lambda _p: asyncio.sleep(0, result=SimpleNamespace(width=3840, height=2160)))
    scene = SimpleNamespace(id="fig", reveal="even", build="sequence", camera="follow", dwell=0.8, motion="drift")
    band = {"top": 0.0, "bottom": 206.4}
    path, clip = asyncio.run(
        figures.draw(
            source,
            document,
            user=None,
            scene=scene,
            length=3.0,
            words=None,
            fps=10,
            viewport=(1920, 1080),
            scale=2,
            workdir=tmp_path,
            safe=band,
        )
    )
    assert (
        opened["url"].endswith(figures.FIGURE_PAGE_PATH)
        and opened["signal"] == "figure"
        and opened["device_scale_factor"] == 2
    )
    planned = next(arg for script, arg in evaluated if "plan" in script)
    assert (
        planned["times"] == figures.reveal_times([["Logo"], ["Caption"]], length=3.0) and planned["camera"] == "follow"
    )
    assert planned["motion"] == "drift"
    assert planned["hold"] == figures.WHOLE_HOLD and planned["safe"] == band
    # Drawn where it moves (0.3 - 1.2 s), and the first frames before it; the rest is held.
    times = [t for t, _name in encoded["frames"]]
    assert times == figures.frame_times([[0.3, 1.2]], 3.0, 10) and encoded["end_time"] == 3.0 and encoded["fps"] == 10
    assert (clip.duration, clip.width, clip.frame_count) == (3.0, 3840, 30) and path.endswith("figure.mp4")

    answer = opened["answer"]
    spec_route = _Route("https://render.local/__figure__/spec.json")
    assert asyncio.run(answer(spec_route)) is True and spec_route.answered["content_type"] == "application/json"
    picture_route = _Route(f"https://render.local/__figure__/asset/{PICTURE_ID}")
    assert asyncio.run(answer(picture_route)) is True and picture_route.answered["body"] == b"png"
    other = _Route(f"https://render.local/__figure__/asset/{uuid.uuid4()}")
    assert asyncio.run(answer(other)) is True and other.answered["status"] == 404
    assert asyncio.run(answer(_Route("https://render.local/assets/index.js"))) is False


def test_a_figure_drawn_again_in_its_working_directory_keeps_the_frames_and_the_video_it_drew(monkeypatch, tmp_path):
    """A render resumed after a lost lease, a crash or a stall draws only the figure frames it had not drawn."""
    from app.services import presentation_export
    from app.services.video_studio import encoder

    source = figures.source_from_spec(_figure(block_spec={"type": "image", "file_id": PICTURE_ID, "alt": "Logo"}))
    document = {"kind": "image", "presentation": {"spec": {}}}
    shot: list[str] = []
    stop_at: list[int] = []

    class _Page:
        async def evaluate(self, script, arg=None):
            if "describe" in script:
                return {"steps": [{"labels": ["Logo"]}, {"labels": ["Caption"]}]}
            if "plan" in script:
                return {"active": [[0.0, arg["length"]]]}
            return None

        async def screenshot(self, *, path, **_kw):
            Path(path).write_bytes(b"jpeg")
            if stop_at and len(shot) == stop_at[0]:
                # The render process killed while it wrote this frame: never renamed, never kept.
                raise RuntimeError("Target page, context or browser has been closed")
            shot.append(Path(path).name)

    opened: list[int] = []

    @asynccontextmanager
    async def open_page(url, **kw):
        opened.append(1)
        yield _Page()

    encoded: list[list] = []

    async def frames_to_mezzanine(frames, *, out_path, **_kw):
        encoded.append(list(frames))
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", lambda _p: asyncio.sleep(0, result=SimpleNamespace(width=1920, height=1080)))
    scene = SimpleNamespace(id="fig", reveal="even", build="sequence", camera="follow", dwell=0.8, motion="drift")
    drawn: list[int] = []

    def draw(length: float = 3.0):
        return asyncio.run(
            figures.draw(
                source,
                document,
                user=None,
                scene=scene,
                length=length,
                words=None,
                fps=10,
                viewport=(1920, 1080),
                scale=1,
                workdir=tmp_path,
                on_frame=lambda: drawn.append(1),
            )
        )

    stop_at.append(12)
    with pytest.raises(figures.FigureError, match="browser has been closed"):
        draw()
    assert len(drawn) == 12 and encoded == []
    folder = tmp_path / "figure-fig"
    assert len(list(folder.glob("f*.jpg"))) == 12 and list(folder.glob("*.part"))

    # Drawn again in the same directory: the 12 frames drawn whole are kept, the one cut short is drawn again.
    stop_at.clear()
    shot.clear()
    drawn.clear()
    path, clip = draw()
    assert shot[0] == "f00012.jpg.part" and len(shot) == len(drawn) == 30 - 12
    assert [name for _t, name in encoded[-1]] == [f"f{index:05d}.jpg" for index in range(30)]
    assert path.endswith("figure.mp4") and clip.frame_count == 30

    # Once more: the video it encoded is kept as it is, nothing drawn or encoded again.
    shot.clear()
    assert draw()[0] == path and shot == [] and len(encoded) == 1

    # Another drawing (the scene grew): every frame drawn again.
    draw(length=4.0)
    assert len(shot) == 40 and len(encoded) == 2


def _failing_page_draw(monkeypatch, tmp_path, *, fail_opens: int, release: str = "1.0.0"):
    """Draw a figure whose page fails on its first *fail_opens* opens, the worker on *release*: (draw, opened)."""
    from app.core.config import settings
    from app.services import presentation_export
    from app.services.video_studio import encoder

    source = figures.source_from_spec(_figure(block_spec={"type": "image", "file_id": PICTURE_ID, "alt": "Logo"}))
    document = {"kind": "image", "presentation": {"spec": {}}}
    opened: list[int] = []

    class _Page:
        async def evaluate(self, script, arg=None):
            if "describe" in script:
                return {"steps": [{"labels": ["Logo"]}]}
            if "plan" in script:
                return {"active": [[0.0, arg["length"]]]}
            return None

        async def screenshot(self, *, path, **_kw):
            Path(path).write_bytes(b"jpeg")

    @asynccontextmanager
    async def open_page(url, *, error, **kw):
        opened.append(1)
        if len(opened) <= fail_opens:
            raise error("The figure could not be drawn: its code did not load, or its drawing failed.")
        yield _Page()

    async def frames_to_mezzanine(frames, *, out_path, **_kw):
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr(settings, "app_version", release)
    monkeypatch.setattr(presentation_export, "open_render_page", open_page)
    monkeypatch.setattr(encoder, "frames_to_mezzanine", frames_to_mezzanine)
    monkeypatch.setattr(encoder, "probe", lambda _p: asyncio.sleep(0, result=SimpleNamespace(width=1920, height=1080)))
    scene = SimpleNamespace(id="fig", reveal="even", build="sequence", camera="follow", dwell=0.8, motion="drift")

    def draw():
        return asyncio.run(
            figures.draw(
                source,
                document,
                user=None,
                scene=scene,
                length=2.0,
                words=None,
                fps=10,
                viewport=(1920, 1080),
                scale=1,
                workdir=tmp_path,
            )
        )

    return draw, opened


def test_a_figure_page_that_did_not_load_is_drawn_again_once_on_a_fresh_page(monkeypatch, tmp_path):
    draw, opened = _failing_page_draw(monkeypatch, tmp_path, fail_opens=1)
    path, clip = draw()
    assert len(opened) == 2 and path.endswith("figure.mp4") and clip.frame_count == 20
    assert (tmp_path / "figure-fig" / figures._RELEASE_FILE).read_text() == "1.0.0"


def test_a_figure_page_failing_twice_on_a_first_draw_stays_the_figure_s_failure(monkeypatch, tmp_path):
    draw, opened = _failing_page_draw(monkeypatch, tmp_path, fail_opens=5)
    with pytest.raises(figures.FigureError, match="its code did not load") as raised:
        draw()
    assert len(opened) == figures.FIGURE_PAGE_ATTEMPTS and not isinstance(raised.value, figures.FigurePageError)
    assert "resume across builds" not in str(raised.value)


def test_a_figure_resumed_on_another_build_whose_page_fails_is_the_platform_s_failure(monkeypatch, tmp_path):
    """The figure bundle missing on the resuming worker: its page fails twice, the render fails as the platform's."""
    folder = tmp_path / "figure-fig"
    folder.mkdir()
    (folder / figures._RELEASE_FILE).write_text("1.0.0", encoding="utf-8")
    (folder / "f00000.jpg").write_bytes(b"jpeg")
    draw, opened = _failing_page_draw(monkeypatch, tmp_path, fail_opens=5, release="1.1.0")
    with pytest.raises(figures.FigurePageError) as raised:
        draw()
    said = str(raised.value)
    assert len(opened) == figures.FIGURE_PAGE_ATTEMPTS and said.startswith("Scene fig: the figure could not be drawn:")
    assert "failed after resume across builds: drawn first on 1.0.0, resumed on 1.1.0" in said


def test_a_figure_resumed_on_another_build_is_drawn_from_this_build_s_page(monkeypatch, tmp_path):
    folder = tmp_path / "figure-fig"
    folder.mkdir()
    (folder / figures._RELEASE_FILE).write_text("1.0.0", encoding="utf-8")
    draw, opened = _failing_page_draw(monkeypatch, tmp_path, fail_opens=0, release="1.1.0")
    path, _clip = draw()
    assert len(opened) == 1 and path.endswith("figure.mp4")
    assert (folder / figures._RELEASE_FILE).read_text() == "1.1.0"


def test_a_figure_page_failure_after_a_resume_runs_the_render_again_as_the_platform_s():
    from app.services.video_studio import compositor, render_retry

    assert render_retry.cause_of_exception(compositor.ChromiumError("x")) == render_retry.CHROMIUM
    assert figures.resume_words("1.0.0", "1.0.0") == "" and figures.resume_words(None, "1.1.0") == ""
    body = (BACKEND_ROOT / "app/services/video_studio/render.py").read_text(encoding="utf-8")
    assert "except figures.FigurePageError as exc:\n" in body and "raise compositor.ChromiumError(str(exc))" in body


def test_a_picture_the_block_does_not_name_is_never_read():
    source = figures.FigureSource("image", {}, None, frozenset({PICTURE_ID}))
    assert asyncio.run(figures._picture(source, None, str(uuid.uuid4()))) is None


def test_the_figure_page_is_public_and_shows_the_block_alone():
    app = (FRONTEND_ROOT / "src/App.tsx").read_text(encoding="utf-8")
    assert f'path="{figures.FIGURE_PAGE_PATH}"' in app
    css = (FRONTEND_ROOT / "src/components/presentation/presentation.css").read_text(encoding="utf-8")
    assert ".deck-root.deck-figure-render" in css and "[data-figure-root]" in css
    page = (FRONTEND_ROOT / "src/pages/VideoFigureStaticRender.tsx").read_text(encoding="utf-8")
    assert "`__figure__/${path}`" in page and 'figureUrl("spec.json")' in page and "figureUrl(`asset/" in page
