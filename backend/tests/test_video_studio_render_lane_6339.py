"""A render lane's real holders, the estimate quoted from the slowest scene type, and a render carried across a
revision that changes nothing it draws. Synthetic rows and specs only.

* the render lane is held by its renders and by the scene previews in a slot it lent - recorded on the execution
  when it is lent -, never by a preview on the preview lane's own slots, which is counted apart; the 22 Oct 2026
  case - one render drawing, one preview on its own lane - reads one holder, not two;
* a resumed render's wait counts from the last check that found its lane held, so the wait it states once its lane
  frees is the wait it gets, and the sweep records each held check and fails only past it;
* a lane takes back a slot it lent for a render a stopped worker left: the preview in it stops between frames,
  keeps its finished frames and goes back first in its own lane;
* every frame is timed under its scene type - a clip with a screen insert, a statement over a clip, a figure -, a
  render and a scene preview record it, the pre-flight reads both, weighs each frame by its scene type and quotes
  the estimate from the slowest one the video holds: the 22 Oct 2026 ultra render said 13 min for 8,462 frames;
* what a whole render draws leaves out what is only heard or judged - a clip's audio flag, an insert's legibility,
  a deck's speaker notes - and a save carries the render in progress across such a revision; the carried render
  mixes the later revision's sound.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_adapters.base import AdapterContext
from app.services.video_studio import (
    compositor,
    jobs,
    lexicon_store,
    preview_lane,
    projects,
    render,
    render_checks,
    render_preflight,
    render_process,
    timeline,
)
from app.services.video_studio.render_process import RenderExit
from app.services.video_studio.runners import JobOutcome
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

UHD = (3840, 2160)
CLIP = "00000000-0000-0000-0000-00000000c1c1"
INSERTED = "00000000-0000-0000-0000-00000000c2c2"
DECK = "00000000-0000-0000-0000-00000000d3d3"
TRACK = "00000000-0000-0000-0000-00000000e4e4"


class _Db:
    async def commit(self):
        return None

    async def flush(self):
        return None


@contextlib.asynccontextmanager
async def _session(db):
    yield db


# ── 1. The render lane's real holders ─────────────────────────────────────


def _resumed(**fields) -> SimpleNamespace:
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "state": "pending",
        "adapter": jobs.RENDER_ADAPTER,
        "adapter_state": jobs.HANDED_OVER_STATE,
        "payload": {jobs.HANDED_OVER_KEY: {"resume_wait_seconds": 600}},
        "last_error": "Handed to the next worker at 62%",
        "updated_at": now - timedelta(minutes=26),
    }
    values.update(fields)
    return SimpleNamespace(**values)


def test_a_scene_preview_on_its_own_lane_holds_no_slot_a_resumed_render_waits_for():
    # The 22 Oct 2026 case: one render drawing, one scene preview on the preview lane's own slot - one holder.
    reported = [(jobs.RENDER_ADAPTER, "w2", None), (jobs.PREVIEW_ADAPTER, "w3", None)]
    assert jobs.lane_counts(jobs.RENDER_ADAPTER, reported) == (1, 1)
    # A preview in a slot the render lane lent holds that slot; every preview holds one of the preview lane.
    lent = [*reported, (jobs.PREVIEW_ADAPTER, "w2", jobs.RENDER_ADAPTER)]
    assert jobs.lane_counts(jobs.RENDER_ADAPTER, lent) == (2, 1)
    assert jobs.lane_counts(jobs.PREVIEW_ADAPTER, lent) == (2, 0)
    now = datetime.now(UTC)
    held = jobs.resume_of(_resumed(), SimpleNamespace(progress_at=None), lane_running=1, previews_running=1)
    said = held.deadline_clause(now)
    assert said.startswith("it waits while 1 job holds a slot of its lane (1 scene preview on its own lane holds none of it)")
    assert said.endswith("once none does, it fails if no worker resumes it within 10 min")
    told = held.as_dict(now)
    assert told["fails_in_seconds"] is None and told["lane_running"] == 1 and told["previews_running"] == 1


def test_the_wait_a_resumed_render_states_once_its_lane_frees_is_the_wait_it_gets():
    now = datetime.now(UTC)
    gone_quiet = SimpleNamespace(progress_at=now - timedelta(minutes=26))
    # Handed over 26 min ago, its lane held until 2 min ago: 8 of its 10 min left, never 0.
    seen = _resumed(payload={jobs.HANDED_OVER_KEY: {"resume_wait_seconds": 600}, jobs.LANE_HELD_KEY: (now - timedelta(minutes=2)).isoformat()})
    freed = jobs.resume_of(seen, gone_quiet)
    assert 479 <= freed.grace_seconds(now) <= 481
    assert "it fails if no worker resumes it in 4" in freed.deadline_clause(now)
    assert freed.as_dict(now)["lane_held_at"] == freed.lane_held_at.isoformat()
    # Never seen held: counted from its hand-over, as before.
    assert jobs.resume_of(_resumed(), gone_quiet).grace_seconds(now) == 0


class _SweepSession:
    """The sweep's reads, in its order - the renders running, then the resumed ones - and its writes."""

    def __init__(self, running, rows):
        self.reads = [running, rows]
        self.writes: list = []

    async def execute(self, statement, *_args, **_kwargs):
        from sqlalchemy.sql.dml import Update

        if isinstance(statement, Update):
            self.writes.append(statement)
            return SimpleNamespace(rowcount=1)
        found = self.reads.pop(0)
        return SimpleNamespace(all=lambda: list(found))

    async def commit(self):
        return None


async def test_the_sweep_records_a_held_lane_and_fails_only_once_the_stated_wait_has_run(monkeypatch):
    now = datetime.now(UTC)
    render_row = SimpleNamespace(id=uuid.uuid4(), progress=62, progress_at=now - timedelta(minutes=26), scene_id=None)
    execution = _resumed()
    cancelled = AsyncMock(return_value=None)
    monkeypatch.setattr(engine, "cancel_execution", cancelled)
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=[]))
    # A render draws: the lane is held - recorded, never failed.
    held = _SweepSession([(jobs.RENDER_ADAPTER, "w2", None)], [(render_row, execution)])
    monkeypatch.setattr(jobs, "async_session", lambda: _session(held))
    assert await jobs.fail_unresumed_renders(now=now) == 0
    [stamp] = held.writes
    assert "orchestration_executions" in str(stamp) and cancelled.await_count == 0
    # Only a scene preview on its own lane runs: it holds nothing, and 26 min after its hand-over it fails.
    alone = _SweepSession([(jobs.PREVIEW_ADAPTER, "w3", None)], [(render_row, execution)])
    monkeypatch.setattr(jobs, "async_session", lambda: _session(alone))
    assert await jobs.fail_unresumed_renders(now=now) == 1
    assert cancelled.await_count == 1
    # Its lane seen held 2 min ago: it has 8 min still.
    recent = _resumed(payload={**execution.payload, jobs.LANE_HELD_KEY: (now - timedelta(minutes=2)).isoformat()})
    waiting = _SweepSession([], [(render_row, recent)])
    monkeypatch.setattr(jobs, "async_session", lambda: _session(waiting))
    assert await jobs.fail_unresumed_renders(now=now) == 0 and cancelled.await_count == 1


async def test_a_claim_into_another_lanes_slot_records_it_and_a_claim_of_its_own_drops_it():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        state="pending",
        payload={"render_id": "r1", engine.NOT_BEFORE_PAYLOAD_KEY: "2026-10-22T10:00:00+00:00"},
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=0,
        updated_at=None,
    )

    class _Claims(_Db):
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    [claimed] = await engine.claim_due(_Claims(), adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1, slot=jobs.RENDER_ADAPTER)
    assert claimed.payload == {"render_id": "r1", engine.LENT_SLOT_PAYLOAD_KEY: jobs.RENDER_ADAPTER}
    row.state = "pending"
    await engine.claim_due(_Claims(), adapter=jobs.PREVIEW_ADAPTER, worker_id="w1", limit=1)
    assert row.payload == {"render_id": "r1"}


async def test_a_lane_takes_back_a_slot_it_lent_for_a_render_a_stopped_worker_left(monkeypatch):
    older, newer = uuid.uuid4(), uuid.uuid4()
    lent = {older: (jobs.RENDER_ADAPTER, asyncio.Event()), newer: (jobs.RENDER_ADAPTER, asyncio.Event())}
    monkeypatch.setattr(reconciler, "_lent", lent)
    monkeypatch.setattr(reconciler, "async_session", lambda: _session(_Db()))
    waiting = AsyncMock(return_value=1)
    monkeypatch.setattr(engine, "count_waiting", waiting)
    lane = adapters.VideoRenderAdapter()
    # One resumed render waiting: the newest lent slot - the preview that drew least - is taken back.
    assert await reconciler._take_back_lent_slots(lane) == 1
    assert lent[newer][1].is_set() and not lent[older][1].is_set()
    assert waiting.await_args.kwargs["adapter_states"] == jobs.RESUMING_STATES
    # Asked already: none more for the same render.
    assert await reconciler._take_back_lent_slots(lane) == 0
    # The preview lane lends nothing, so takes nothing back.
    assert await reconciler._take_back_lent_slots(adapters.VideoPreviewAdapter()) == 0


async def test_a_preview_whose_lent_slot_is_taken_back_hands_it_back_first_in_its_lane(monkeypatch, tmp_path: Path):
    marked = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "mark_if_open", marked)
    monkeypatch.setattr(compositor, "frames_kept", lambda _workdir: 120)
    outcome = await render_process.hand_over("r1", RenderExit(kind="handed_over", progress=40), tmp_path, given_back=True)
    assert outcome.kind == "given_back" and outcome.kind in render_process._RUNS_AGAIN
    assert outcome.error.startswith("Gave its slot back at 40% to a whole render a stopped worker left")
    assert "resumes first in the preview lane from its 120 finished frames" in marked.await_args.kwargs["stage"]
    # It stops on the stop or on the give-back, whichever comes.
    give_back = asyncio.Event()
    leaving = render_process._Leaving(None, give_back)
    assert not leaving.is_set()
    give_back.set()
    assert leaving.is_set()
    # The adapter hands it back first in its own lane, its attempt unspent.
    seen: dict = {}

    async def run_isolated(_job_id, **kwargs):
        seen.update(kwargs)
        return JobOutcome("given_back", outcome.error)

    monkeypatch.setattr(render_process, "run_isolated", run_isolated)
    monkeypatch.setattr(jobs, "preview_waits", AsyncMock(return_value=None))
    execution = SimpleNamespace(id=uuid.uuid4(), payload={"render_id": "r1"}, adapter_state=None, attempt_count=1, max_attempts=3)
    ctx = AdapterContext(db=_Db(), worker_id="w1", give_back=give_back)
    result = await adapters.VideoRenderAdapter().execute(ctx, execution)
    assert seen["give_back"] is give_back
    assert result.kind == "defer" and result.adapter_state == jobs.SLOT_GIVEN_BACK_STATE and result.place == "head"
    assert preview_lane.first_in_line(SimpleNamespace(adapter_state=jobs.SLOT_GIVEN_BACK_STATE))


# ── 2. Each frame by its scene type, the estimate from the slowest ──────


def test_a_frame_is_timed_under_its_scene_type():
    assert render_preflight.scene_class("clip", inserted=True) == "clip+insert"
    assert render_preflight.scene_class("statement", backdrop="clip") == "statement+clip"
    assert render_preflight.scene_class("figure") == "figure"
    assert render_preflight.scene_words("clip+insert") == "clip with a screen insert"
    assert render_preflight.scene_words("statement+clip") == "statement over a clip"
    insert = SimpleNamespace(type="screen_insert")
    spec = SimpleNamespace(
        scenes=[
            SimpleNamespace(id="shot", type="clip", overlays=[insert]),
            SimpleNamespace(id="claim", type="statement", backdrop=SimpleNamespace(clip_id=CLIP), overlays=[]),
            SimpleNamespace(id="chart", type="figure", overlays=[]),
        ]
    )
    classes = render_preflight.scene_classes(spec)
    assert classes == {"shot": "clip+insert", "claim": "statement+clip", "chart": "figure"}
    document = {
        "fps": 10,
        "frames": 90,
        "scenes": [
            {"id": "shot", "type": "clip", "start": 0.0, "end": 3.0},
            {"id": "claim", "type": "statement", "start": 3.0, "end": 6.0, "transition_in": {"duration": 0.5}},
            # A figure drawn plays as a shot in the planned document: the spec's type is the one timed.
            {"id": "chart", "type": "clip", "start": 6.0, "end": 9.0},
        ],
    }
    kinds = render_preflight.FrameKinds.of(document, classes=classes)
    assert [kinds.scene(frame) for frame in (0, 31, 70)] == ["clip+insert", "statement+clip", "figure"]
    assert kinds.scene_counts()[("transition", "statement+clip")] == 5
    tracker = compositor.DrawProgress()
    tracker.timed("plain", 5.6, 0, scene="clip+insert")
    tracker.timed("plain", 0.2, 1, scene="figure")
    assert tracker.scenes() == {"clip+insert": [1, 5.6], "figure": [1, 0.2]}
    measured = render_preflight.measurement(render_preflight.Layout(pages=2, cpus=4, wanted=2), frames=2, seconds=6.0, scenes=tracker.scenes())
    assert measured["scenes"] == {"clip+insert": [1, 5.6], "figure": [1, 0.2]}
    assert render_preflight.scene_timings(measured)[0] == ("clip+insert", 1, 5.6)


def _ultra_film(clip_frames: int = 1240) -> render_preflight.FrameKinds:
    """8,462 frames of 3840x2160 at 60 fps: titles and statements, then two clip scenes with a screen insert."""
    fps, frames = 60.0, 8462
    cards = (frames - clip_frames) / fps
    clips = clip_frames / fps
    return render_preflight.FrameKinds(
        fps=fps,
        frames=frames,
        scenes=(
            (0.0, cards / 2, "title"),
            (cards / 2, cards, "statement"),
            (cards, cards + clips / 2, "clip+insert"),
            (cards + clips / 2, cards + clips, "clip+insert"),
        ),
    )


def test_the_estimate_is_quoted_from_the_slowest_scene_type_the_video_holds():
    film = _ultra_film()
    # The fleet's average frame, fast enough for the 13 min the pre-flight of 22 Oct 2026 said.
    fleet = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE * 3)
    average = render_preflight.estimate(frames=film.frames, size=UHD, quality="ultra", pages=4, cpus=8, rate=fleet, kinds=film)
    assert 10 * 60 < average.seconds < 16 * 60
    # A clip with a screen insert measured at 5.6 s a frame on a page (1 % per 8 min of that render).
    cost = render_preflight.PIXEL_COST["ultra"] * UHD[0] * UHD[1]
    rates = dataclasses.replace(fleet, scene_rates={"clip+insert": cost / 5.6}, scene_measured={"clip+insert": 2})
    said = render_preflight.estimate(frames=film.frames, size=UHD, quality="ultra", pages=4, cpus=8, rate=rates, kinds=film)
    assert said.slowest == "clip+insert" and said.slowest_seconds == pytest.approx(5.6)
    # The 4 pages share the clip scenes' 1,240 frames at 5.6 s each: the render takes at least a page's share
    # of them, never the fleet's average frame, and less than one page drawing them all.
    assert 1240 * 5.6 / 4 <= said.seconds < 1240 * 5.6
    line = said.line()
    assert "its slowest scenes (clip with a screen insert, 1,240 frames) at 5.60 s a frame on each of 4 pages (8 CPUs)" in line
    assert "measured on 2 renders or previews of this platform" in line
    assert "By scene type: 1,240 frames of clip with a screen insert at 5.60 s each on a page" in line
    kept = said.as_dict()
    assert kept["slowest"] == "clip+insert" and kept["model"] == render_preflight.ESTIMATE_MODEL >= 3
    told = render_preflight.said_of(SimpleNamespace(preflight=kept, measured=None))
    assert "Its slowest scenes, clip with a screen insert (1,240 frames), were expected at 5.60 s a frame on a page" in told


def test_a_scene_types_cost_is_read_from_the_renders_and_previews_that_drew_it():
    preview = {"pages": 1, "cpus": 8, "frames": 300, "seconds": 900.0, "scenes": {"clip+insert": [240, 840.0]}}
    whole = {
        "pages": 4,
        "cpus": 8,
        "frames": 7200,
        "seconds": 3000.0,
        "scenes": {"title": [3600, 900.0]},
        "figure_frames": 600,
        "figure_seconds": 300.0,
    }

    class _Renders:
        def __init__(self, renders, previews):
            self.renders, self.previews = renders, previews

        async def execute(self, statement, _params=None):
            rows = self.previews if "video_renders.scene_id IS NOT NULL" in str(statement) else self.renders
            return SimpleNamespace(all=lambda: list(rows))

    renders = [("ultra", 3840, 2160, 120_000, whole, None)]
    previews = [("draft", 1280, 720, 6000, preview, "shot")]
    rates = asyncio.run(render_preflight.measured_rates(_Renders(renders, previews), "ultra"))
    # The preview drew on 1 page of 8 CPUs - contended -, the rate returned is of full pages.
    drew = 240 / 840.0 * 1280 * 720 * render_preflight.PIXEL_COST["draft"] / render_preflight.DEFAULT_CONTENTION
    assert rates.contended is False and rates.scene_rates["clip+insert"] == pytest.approx(drew)
    assert rates.scene_rates["title"] == pytest.approx(3600 / 900.0 * 3840 * 2160 * render_preflight.PIXEL_COST["ultra"])
    assert rates.scene_measured == {"clip+insert": 1, "title": 1}
    assert rates.figure_rate == pytest.approx(600 / 300.0 * 3840 * 2160 * render_preflight.PIXEL_COST["ultra"])
    assert rates.scene_basis("clip+insert") == "measured on 1 render or preview of this platform"
    # A platform whose previews alone measured it: the estimate of its first whole render reads them.
    first = asyncio.run(render_preflight.measured_rates(_Renders([], previews), "ultra"))
    assert first.page_rate == render_preflight.DEFAULT_PAGE_RATE and "clip+insert" in first.scene_rates


# ── 3. A render carried across a revision that changes nothing it draws ──


class _Media:
    """The rows a digest reads: the shot, and the deck as it is now."""

    def __init__(self, deck=None):
        self.deck = deck

    async def get(self, model, _key, **_options):
        if model.__name__ == "Presentation":
            return self.deck
        return None


def _deck(*, notes: str = "Say the number.", title: str = "Q3", revision: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        revision=revision, spec={"slides": [{"title": title, "notes": notes}]}, theme="dark", template_container_id=None
    )


def _film(
    *, audio: bool | None = False, legibility: str | None = None, opening: str = "Hello", voice: str = "marin", music=None, deck=True
) -> dict:
    overlay = {
        "type": "screen_insert",
        "source": {"clip_id": INSERTED},
        "quad": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]],
        "start": 0.5,
        "duration": 2.0,
    }
    if legibility:
        overlay["legibility"] = legibility
    shot = {"id": "shot", "type": "clip", "clip_id": CLIP, "overlays": [overlay]}
    if audio is not None:
        shot["audio"] = audio
    scenes = [{"id": "open", "type": "title", "title": opening, "duration": 3}, shot]
    if deck:
        scenes.append({"id": "deck", "type": "slide", "presentation_id": DECK, "slide": 1})
    spec = {"title": "Carry", "freestyle": True, "voiceover": {"voice": voice}, "scenes": scenes}
    if music is not None:
        spec["music"] = music
    return validate_video_spec(spec)


def _marks(effective: dict, deck=None) -> render_checks.SceneDigests:
    return asyncio.run(render_checks.digest_scenes(_Media(deck or _deck()), effective))


def test_what_a_render_draws_leaves_out_what_is_only_heard_or_judged():
    base = _marks(_film())
    assert set(base.drawn) == {"open", "shot", "deck"}
    # A clip's own sound: heard, never drawn - the whole digest (its preview's) sees it, the drawing does not.
    heard = _marks(_film(audio=True))
    assert heard.drawn == base.drawn and heard.whole["shot"] != base.whole["shot"]
    # A screen insert's legibility declaration says how it is judged.
    assert _marks(_film(legibility="ambient")).drawn == base.drawn
    # A deck's speaker notes: its revision moves, its slides as an audience sees them do not.
    noted = _marks(_film(), _deck(notes="Pause on the chart.", revision=2))
    assert noted.drawn == base.drawn and noted.whole["deck"] != base.whole["deck"]
    assert _marks(_film(), _deck(title="Q4", revision=2)).drawn["deck"] != base.drawn["deck"]
    # What is drawn: a title's words, and the voice that times every narration and its captions.
    assert _marks(_film(opening="Welcome")).drawn["open"] != base.drawn["open"]
    voiced = _marks(_film(voice="cedar"))
    assert voiced.whole == base.whole and voiced.drawn != base.drawn
    # The music's file is gathered before a frame is drawn: a carried render must keep it.
    assert render_checks.sound_sources(_film()) != render_checks.sound_sources(_film(music={"music_id": TRACK}))
    assert render_checks.sound_sources(_film()) == render_checks.sound_sources(_film(audio=True))


@pytest.fixture
def carrying(monkeypatch):
    async def own_lexicon(_db, _voiceover):
        return None

    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)


def _in_progress(effective: dict | None, *, started: bool = True, revision: int = 12) -> SimpleNamespace:
    drawing = None
    if effective is not None:
        marks = _marks(effective)
        drawing = {"revision": revision, "scenes": marks.drawn, "whole": marks.whole, "sources": render_checks.sound_sources(effective)}
    return SimpleNamespace(
        id=uuid.uuid4(),
        revision=revision,
        quality="ultra",
        progress=34,
        started_at=datetime.now(UTC) if started else None,
        drawing=drawing,
    )


def test_a_save_carries_a_render_in_progress_whose_frames_it_does_not_change(carrying):
    project = SimpleNamespace(id=uuid.uuid4(), revision=13)
    db = _Media(_deck())

    def carry(renders, before, after):
        return asyncio.run(projects.carry_renders(db, project, renders, before=before, after=after, from_revision=12))

    # Revision 13 turns the clip's own sound on: nothing drawn changed, the render is kept and is the render of 13.
    drawing = _in_progress(_film())
    [kept] = carry([drawing], None, _film(audio=True))
    assert kept.render is drawing and kept.from_revision == 12 and drawing.revision == 13
    assert drawing.drawing["carried_from"] == 12 and drawing.drawing["revision"] == 13
    assert drawing.drawing["spec"]["scenes"][1]["audio"] is True
    assert projects.carried_from(drawing) == 12
    line = projects.carried_line([kept], 13)
    assert "draws exactly what revision 13 draws" in line and "instead of being cancelled and drawn again" in line
    # Carried again by a later revision that draws the same: still the revision it was asked for.
    project.revision = 14
    [again] = asyncio.run(
        projects.carry_renders(db, project, [drawing], before=None, after=_film(audio=True, legibility="ambient"), from_revision=13)
    )
    assert again.from_revision == 12 and drawing.revision == 14
    project.revision = 13
    # A title's words, the music's file: drawn, or gathered before drawing - left to be cancelled and drawn again.
    assert carry([_in_progress(_film())], None, _film(opening="Welcome")) == []
    assert carry([_in_progress(_film())], None, _film(music={"music_id": TRACK})) == []
    # A render that started before it recorded what it draws is never carried.
    assert carry([_in_progress(None)], _film(), _film(audio=True)) == []
    # A queued one reads the spec when it starts: compared on the revision it was asked for.
    queued = _in_progress(None, started=False)
    assert [item.render for item in carry([queued], _film(), _film(audio=True))] == [queued] and queued.revision == 13
    assert carry([_in_progress(None, started=False)], _film(), _film(opening="Welcome")) == []


def test_a_carried_render_mixes_the_sound_of_the_revision_it_finishes_for():
    drawn_with = _film(audio=False, deck=False)
    spec = parse_video_spec(drawn_with)
    spec = spec.model_copy(update={"scenes": [spec.scenes[0], spec.scenes[1].model_copy(update={"overlays": []})]})
    media = render._Media()
    media.plan = timeline.PlanMedia(clips={"shot": timeline.ClipMedia(6.0, 1920, 1080, 24.0, 144, has_audio=True)})
    plan = timeline.plan(spec, media.plan, fps=30, theme={})
    assert plan.clip_audio == []
    later = _film(audio=True, deck=False)
    later["scenes"][1]["overlays"] = []
    later["voiceover"]["volume"] = 1.4
    heard, mixed = render._sound_of({"spec": later, "revision": 13}, spec, plan, media, fps=30, theme={})
    # Its clip's sound plays, at the later revision's levels, over the same frames.
    assert [sound.scene_id for sound in mixed.clip_audio] == ["shot"]
    assert mixed.document is plan.document and heard.voiceover.volume == 1.4
    assert [scene.id for scene in heard.scenes] == ["open", "shot"]


async def test_render_video_says_the_render_it_follows_was_carried_from_an_earlier_revision():
    carried = SimpleNamespace(id=uuid.uuid4(), revision=13, drawing={"carried_from": 12, "revision": 13}, owed_conversation_id=None)
    started = projects._reused(carried, None)
    assert started.reused and started.carried_from == 12
    # Its own revision, never carried: nothing to say.
    assert projects._reused(SimpleNamespace(revision=13, drawing=None, owed_conversation_id=None), None).carried_from is None


def test_an_attempt_records_what_it_draws_keeping_the_revision_it_was_first_asked_for():
    marks = render_checks.SceneDigests({"a": "w"}, {"a": "c"}, {"a": "d"}, "h")
    # A new attempt of a render carried from 11 to 12 reads revision 12 itself: still asked for 11.
    carried = SimpleNamespace(drawing={"revision": 12, "carried_from": 11, "spec": {"title": "Carry"}})
    assert render.drawing_of(carried, 12, marks, "s") == {
        "revision": 12,
        "scenes": {"a": "d"},
        "whole": {"a": "w"},
        "sources": "s",
        "heard": "h",
        "said": {},
        "beside": "",
        "picture": {},
        "release": render_checks.release_drawing(),
        "carried_from": 11,
    }
    # One that reads another revision than it was carried to starts afresh, as does a render's first attempt.
    assert "carried_from" not in render.drawing_of(carried, 13, marks, "s")
    assert render.drawing_of(SimpleNamespace(drawing=None), 12, marks, "s")["revision"] == 12
