"""A render's pages share its frames by what they cost, and idle pages take what the others have left.

The 22 Oct 2026 ultra render of a 136 s video (8,462 frames at 2160p60, 8 pages) drew its 7,100 plain frames in about
15 min, then the 780 frames of its two screen-insert scenes for over 40 min on the 2 pages whose runs held them, 6
pages idle, "running" at 81-94% with no end time:

* its frames replayed at what they cost: cut into one run a page by frame count, 6 pages idle for more than a
  minute; under the schedule, no page idles more than 60 s while frames remain, and it ends about half an hour sooner;
* each page draws its own run first, then the last piece of the run with the most work left, then the second half of
  the piece with the most work left, cut on a frame - never a piece too small to be worth an encoder;
* pages drawing side by side while their pieces are cut under them draw every frame once, and the segments tile the
  range;
* a page starting inside a matted insert anywhere but where it left off draws the frames before it first;
* a kind is priced at what this render drew of it, else at its prior, else at its default share;
* the status line says the frames left, the frames a second over the last minute and the time to go.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from datetime import UTC, datetime

import pytest

from app.services.video_studio import compositor, page_schedule, render_preflight, screen_insert, surface_refine
from app.services.video_studio.page_schedule import FrameCosts, FramePrices, PageWork, Piece

FPS = 60
#: The 22 Oct render: 8,462 frames, two screen-insert scenes of 360 and 420 frames, 18 crossfades of 0.6 s.
FRAMES = 8462
INSERTS = ((70.0, 76.0), (100.0, 107.0))
CROSSINGS = tuple((start, start + 0.6) for start in range(6, 136, 7))[:18]
#: What a frame cost a page: about a second for a plain one (7,100 in 15 min on 8 pages), six for an insert's (780 in
#: 40 min on 2), and an encoder opened and flushed for each segment.
COSTS = FrameCosts(
    {"plain": 1.0, "transition": 1.2, "insert": 6.2, "transition_insert": 6.5},
    segment=4.0,
    sources=dict.fromkeys(render_preflight.FRAME_KINDS, "video"),
)


def _kinds() -> render_preflight.FrameKinds:
    return render_preflight.FrameKinds(float(FPS), FRAMES, CROSSINGS, INSERTS)


def _segments() -> list[tuple[int, int]]:
    return compositor.plan_segments(0, FRAMES, compositor.segment_frames(FRAMES, FPS), [])


def _work(pages: int = 8, *, warm=()) -> PageWork:
    return PageWork(_segments(), pages, FramePrices(_kinds(), 0, FRAMES), COSTS, warm=warm)


def test_the_frames_of_each_kind_are_counted_as_their_kind_says():
    kinds = _kinds()
    prices = FramePrices(kinds, 0, FRAMES)
    assert kinds.labels(0, FRAMES) == [kinds.kind(frame) for frame in range(FRAMES)]
    assert prices.counts(0, FRAMES)["insert"] + prices.counts(0, FRAMES)["transition_insert"] == 780
    assert prices.counts(4200, 4210) == kinds.counts(4200, 4210)
    # A range costs its frames at their kind's price; the cut is where the range reaches its share.
    assert prices.cost(4200, 4210, COSTS) == pytest.approx(62.0)
    middle = prices.cut(4200, 4560, prices.cost(4200, 4560, COSTS) / 2, COSTS)
    assert prices.cost(4200, middle - 1, COSTS) < prices.cost(4200, 4560, COSTS) / 2 <= prices.cost(4200, middle, COSTS)


def test_the_22_oct_render_idles_no_page_more_than_60_seconds_while_frames_remain():
    prices = FramePrices(_kinds(), 0, FRAMES)
    # Before: one contiguous run a page, by frame count, each page drawing its own run alone.
    before = [
        sum(prices.cost(a, b, COSTS) + COSTS.segment for a, b in run) for run in compositor.page_runs(_segments(), 8)
    ]
    idle_before = [max(before) - seconds for seconds in before]
    assert sum(1 for idle in idle_before if idle > 60) >= 6, before
    # After: the same frames under the schedule.
    finish = page_schedule.simulate(_work())
    idle = [max(finish) - seconds for seconds in finish]
    assert max(idle) <= 60, finish
    # Every page busy to the end: the render takes its frames' cost shared by 8, its seams and pre-rolls aside.
    assert max(finish) <= prices.cost(0, FRAMES, COSTS) / 8 * 1.05, finish
    assert max(before) - max(finish) > 25 * 60, (before, finish)


def test_the_segments_are_cut_and_shared_out_by_what_they_cost():
    # Frames 1200-2400 show a screen insert: six times a plain frame each.
    prices = FramePrices(render_preflight.FrameKinds(float(FPS), 2400, (), ((20.0, 40.0),)), 0, 2400)
    segments = [(0, 600), (600, 1200), (1200, 1800), (1800, 2400)]
    work = PageWork(segments, 4, prices, COSTS)
    share = prices.cost(0, 2400, COSTS) / 4
    # The insert segments each cost more than a page's share: cut on a frame, none costing more than the share.
    assert len(work.pieces) > len(segments)
    assert all(work.cost(piece.first, piece.end) <= share + COSTS.of("insert") for piece in work.pieces)
    runs = [sum(work.cost(piece.first, piece.end) for piece in queue) for queue in work.queues]
    assert len(runs) == 4 and max(runs) <= 1.5 * min(runs), runs
    # By frame count, the page drawing the last 600 frames would have had 3.7 times its share.
    assert max(prices.cost(a, b, COSTS) for a, b in segments) > 1.7 * share


def test_an_idle_page_takes_a_piece_no_page_started_then_cuts_the_one_with_most_work_left():
    prices = FramePrices(render_preflight.FrameKinds(float(FPS), 2400, ()), 0, 2400)
    work = PageWork([(0, 600), (600, 1200), (1200, 1800), (1800, 2400)], 2, prices, COSTS)
    assert [[piece.span for piece in queue] for queue in work.queues] == [[(0, 600), (600, 1200)], [(1200, 1800), (1800, 2400)]]
    assert work.take(0).span == (0, 600) and work.take(1).span == (1200, 1800)
    # Its own run first.
    assert work.take(0).span == (600, 1200) and work.moved == 0
    # Then the last piece of the run with the most work left, no page having started it.
    stolen = work.take(0)
    assert stolen.span == (1800, 2400) and stolen.page == 0 and work.moved == 1 and not work.queues[1]
    # Every piece started: the second half of the piece with the most work left, cut on a frame.
    drawing = work.drawing[1]
    drawing.next = 1210
    taken = work.take(0)
    assert taken is not None and work.cut_from == 1 and work.cuts == 1
    assert taken.end == 1800 and drawing.end == taken.first and drawing.next == 1210
    # Each side about half of what was left, the taker's encoder counted on its side.
    kept, given = work.cost(drawing.next, drawing.end), work.cost(taken.first, taken.end)
    assert kept == pytest.approx(given + COSTS.segment, abs=COSTS.of("plain"))
    assert work.plan() == [(0, 600), (600, 1200), (1200, taken.first), (taken.first, 1800), (1800, 2400)]


def test_nothing_too_small_to_be_worth_an_encoder_is_cut():
    prices = FramePrices(render_preflight.FrameKinds(float(FPS), 1200, ()), 0, 1200)
    work = PageWork([(0, 600), (600, 1200)], 2, prices, COSTS)
    drawing, other = work.take(0), work.take(1)
    assert drawing.span == (0, 600) and other.span == (600, 1200)
    other.next = other.end
    # 12 plain frames left: the taker's half would hold 4 s of frames beside its 4 s encoder - not worth it.
    drawing.next = drawing.end - 12
    assert work.take(1) is None and drawing.end == 600
    # 30 left: 13 s each side, cut.
    drawing.next = drawing.end - 30
    taken = work.take(1)
    assert taken is not None and drawing.end == taken.first and taken.end == 600
    # Three frames cannot leave two on each side.
    drawing.next = drawing.end - 3
    assert work.take(1) is None


async def test_pages_drawing_while_their_pieces_are_cut_draw_every_frame_once():
    kinds = render_preflight.FrameKinds(float(FPS), 3000, ((10.0, 10.6),), ((30.0, 42.0),))
    prices = FramePrices(kinds, 0, 3000)
    work = PageWork(compositor.plan_segments(0, 3000, 600, []), 4, prices, COSTS)
    drawn: dict[int, int] = {}
    pieces: list[Piece] = []
    rng = random.Random(6337)  # noqa: S311 - jitter of a test's frame times

    async def page(index: int) -> None:
        while (piece := work.take(index)) is not None:
            pieces.append(piece)
            while piece.next < piece.end:
                frame = piece.next
                piece.next += 1
                assert frame not in drawn
                drawn[frame] = index
                # A thousandth of what the frame costs, off by up to a factor of two: the prices are never exact.
                await asyncio.sleep(COSTS.of(kinds.kind(frame)) / 1000 * rng.uniform(0.5, 2.0))

    await asyncio.gather(*(page(index) for index in range(work.pages)))
    assert sorted(drawn) == list(range(3000)) and work.cuts > 0
    plan = work.plan()
    assert plan[0][0] == 0 and plan[-1][1] == 3000
    assert all(a[1] == b[0] for a, b in itertools.pairwise(plan))
    # Each segment is drawn by one page.
    for piece in pieces:
        assert {drawn[frame] for frame in range(piece.first, piece.end)} == {piece.page}


def test_a_page_starting_inside_a_matted_insert_elsewhere_draws_the_frames_before_it():
    assert page_schedule.PREROLL_FRAMES == surface_refine.PRIOR_SETTLE_FRAMES > screen_insert.MATTE_WINDOW // 2
    work = _work(warm=[(4200, 4560)])
    assert work.preroll(3, 4300) == page_schedule.PREROLL_FRAMES
    assert work.preroll(3, 4202) == 2
    assert work.preroll(3, 4200) == 0 and work.preroll(3, 5000) == 0
    work.left_off[3] = 4300
    assert work.preroll(3, 4300) == 0


def test_a_kind_is_priced_at_what_the_render_drew_of_it_else_its_prior_else_its_default_share():
    prior = FrameCosts({"plain": 0.9, "insert": 5.0}, segment=4.0, sources={"plain": "platform", "insert": "video"})
    priced = FrameCosts.priced(prior, {"plain": (page_schedule.MIN_DRAWN_FRAMES, 33.0), "insert": (10, 100.0)})
    assert priced.of("plain") == pytest.approx(1.1) and priced.sources["plain"] == "drawn"
    # Ten insert frames are not enough to price the kind: the previous render of the video's still is.
    assert priced.of("insert") == 5.0 and priced.sources["insert"] == "video"
    transition = render_preflight.DEFAULT_KIND_COST["transition"] / render_preflight.DEFAULT_KIND_COST["plain"]
    assert priced.of("transition") == pytest.approx(1.1 * transition) and priced.sources["transition"] == "default"
    assert priced.segment == 4.0
    assert FrameCosts.priced().of("plain") == pytest.approx(page_schedule.NOMINAL_SECONDS)


def test_the_status_line_says_the_frames_left_the_pace_over_the_last_minute_and_the_time_to_go():
    rate = page_schedule.Rate()
    for second in range(0, 181, 2):
        rate.note(float(second), second * (2 if second < 120 else 5))
    assert rate.per_second() == pytest.approx(5.0, rel=0.05)
    work = _work()
    left = work.seconds_left()
    assert left == pytest.approx(max(page_schedule.simulate(work)))
    now = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
    said = page_schedule.status_line(87, left=1104, per_second=rate.per_second(), seconds_left=150, now=now)
    assert said == "Drawing frames (87%): 1,104 left, 5.0 frames/s over the last minute, ETA 17:02 UTC (in about 2 min 30 s)"
    assert page_schedule.status_line(3, left=10, per_second=None, seconds_left=None) == "Drawing frames (3%): 10 left"
    # The ETA comes down as the pages draw.
    for page in range(work.pages):
        piece = work.take(page)
        piece.next = min(piece.end, piece.first + 300)
    assert work.seconds_left() < left


def test_the_estimate_replays_the_schedule():
    rates = render_preflight.Rates(render_preflight.DEFAULT_PAGE_RATE)
    kinds = _kinds()
    finish = render_preflight.page_run_seconds(kinds, rates, size=(3840, 2160), quality="ultra", pages=8, cpus=8)
    assert len(finish) == 8 and max(finish) - min(finish) <= 60
    costs = render_preflight.page_costs(rates, size=(3840, 2160), quality="ultra", pages=8, cpus=8)
    assert costs.of("insert") > costs.of("plain") and costs.segment > 0
