"""An AI shot whose take kept no text check is read by the video's quality check (dev:invented-text-gate-skips-null-text-check).

A take made before the text check existed, or whose check was skipped, kept
``frame_qc.text_check`` null: the quality check passed it in silence, though it
showed gibberish signage. Now its frames are read for invented lettering by
the text check's rule (``text_backfill``), and each such take is said: an
``invented_text`` finding with its crop, or a facts line saying it was read, or
that the check did not run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.video_studio import text_backfill, text_drift, timeline
from app.services.video_studio.quality import ContentFacts, ShotFacts, backfill_text_checks, content_findings
from app.services.video_studio.render_checks import BLOCKING_SEVERITIES
from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

pytestmark = pytest.mark.usefixtures("no_default_video_template")


@pytest.fixture(autouse=True)
def _no_reads_held():
    """Each test starts with no read of this process held (``text_backfill._READS``)."""
    text_backfill._READS.clear()
    yield
    text_backfill._READS.clear()

SHOT = "00000000-0000-0000-0000-00000000c001"
TAKE = "00000000-0000-0000-0000-00000000f001"
CROP = "00000000-0000-0000-0000-00000000f101"
SIGN = (0.6, 0.2, 0.2, 0.016)  # about 17 px tall at 1080p: above the 10 px threshold


def _raw() -> dict:
    return {"title": "Synthetic", "freestyle": True, "scenes": [{"id": "street", "type": "clip", "clip_id": SHOT}]}


def _findings(facts: ContentFacts) -> list:
    raw = _raw()
    spec = parse_video_spec(validate_video_spec(raw))
    shots = {SHOT: timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)}
    document = timeline.estimate_plan(spec, {}, shots).document
    return content_findings(spec, document, facts, authored_spec(raw))


def _facts(**changes) -> ContentFacts:
    shot = ShotFacts("Street", "ready", width=1920, height=1080, camera="unspecified", motion_ssim=0.9, **changes)
    return ContentFacts(shots={SHOT: shot}, output=(1920, 1080))


def _reading(**frames: list[text_drift.Word]) -> text_drift.Reading:
    read = {name: frames.get(name, []) for name in text_drift.FRAMES}
    return text_drift.Reading({n: " ".join(w.text for w in ws) for n, ws in read.items()}, dict.fromkeys(read, 0), read)


def test_a_take_without_a_text_check_record_is_read_and_one_with_it_is_not():
    take = text_backfill.unchecked_of({"start_lettering": {"words": ["Filigran"]}}, TAKE, 4000)
    assert take == text_backfill.Unchecked(TAKE, 4.0, ("Filigran",))
    assert text_backfill.unchecked_of({"frame_qc": {"text_check": None}}, TAKE, None) == text_backfill.Unchecked(TAKE)
    # A take whose own check stands, or with no file, is not read again.
    checked = {"frame_qc": {"text_check": {"status": "passed", "invented": []}}}
    assert text_backfill.unchecked_of(checked, TAKE, 4000) is None
    assert text_backfill.unchecked_of({}, None, 4000) is None


def test_the_rule_is_the_text_checks_against_the_start_pictures_words():
    frames = _reading(
        middle_frame=[
            text_drift.Word("Brovantek", 0.9, 0.016, SIGN, False),  # invented, 17 px at 1080p
            text_drift.Word("Filigran", 0.9, 0.02, SIGN, False),  # a word of the start picture
            text_drift.Word("Zuplor", 0.9, 0.005, SIGN, False),  # 5 px: the unreadable count's
            text_drift.Word("Coffee", 0.9, 0.02, SIGN, True),  # a known word
        ]
    )
    invented = text_backfill.judge(frames, ("Filigran",), brand={"xtm"})
    assert invented == (text_drift.Invented("Brovantek", "middle_frame", SIGN),)


def test_path_1_null_text_check_with_invented_lettering_is_a_blocking_finding_with_crop_and_frame():
    facts = _facts(unchecked=text_backfill.Unchecked(TAKE, 4.0))
    asked: dict = {}

    async def read(user_id, takes):
        asked["user"], asked["takes"] = user_id, takes
        return {SHOT: text_backfill.Backfill(True, (text_drift.Invented("Brovantek", "middle_frame", SIGN, CROP),))}

    lines = asyncio.run(backfill_text_checks("u1", facts, read=read))
    assert asked == {"user": "u1", "takes": {SHOT: text_backfill.Unchecked(TAKE, 4.0)}}
    assert lines == [
        f"Text check of clip `{SHOT}` read here (its take kept none): 1 invented word(s): "
        f'"Brovantek" (middle frame, x 0.60-0.80, y 0.20-0.22, crop `{CROP}`).'
    ]
    [finding] = [f for f in _findings(facts) if f.kind == "invented_text"]
    assert finding.severity == "dense" and finding.severity in BLOCKING_SEVERITIES and finding.scene_id == "street"
    assert f'"Brovantek" (middle frame, x 0.60-0.80, y 0.20-0.22, crop `{CROP}`)' in finding.message


def test_path_2_null_text_check_without_lettering_is_no_finding_and_a_facts_line_saying_it_was_read():
    facts = _facts(unchecked=text_backfill.Unchecked(TAKE, 4.0))

    async def read(_user, _takes):
        return {SHOT: text_backfill.Backfill(True)}

    lines = asyncio.run(backfill_text_checks("u1", facts, read=read))
    said = f"Text check of clip `{SHOT}` read here (its take kept none)"
    assert lines == [f"{said}: first, middle and last frames, no invented lettering."]
    assert not [f for f in _findings(facts) if f.kind == "invented_text"]


def test_a_take_that_cannot_be_read_is_said_never_passed_in_silence():
    facts = _facts(unchecked=text_backfill.Unchecked(TAKE))

    async def unreadable(_user, _takes):
        return {SHOT: text_backfill.Backfill(False, why="the reading came back unusable")}

    assert asyncio.run(backfill_text_checks("u1", facts, read=unreadable)) == [
        f"Text check not run on clip `{SHOT}`: the reading came back unusable."
    ]

    async def broken(_user, _takes):
        raise RuntimeError("no model")

    assert asyncio.run(backfill_text_checks("u1", _facts(unchecked=text_backfill.Unchecked(TAKE)), read=broken)) == [
        f"Text check not run on clip `{SHOT}` yet: its frames are still being read past this check's budget, "
        "the next check says what they show."
    ]


def test_path_3_a_take_with_its_text_check_behaves_as_before_and_is_not_read():
    facts = _facts(invented=(text_drift.Invented("Qorvex", "first_frame", SIGN, CROP),))

    async def read(_user, _takes):
        raise AssertionError("a take with its own text check is never read again")

    assert asyncio.run(backfill_text_checks("u1", facts, read=read)) == []
    [finding] = [f for f in _findings(facts) if f.kind == "invented_text"]
    assert finding.severity == "dense" and '"Qorvex"' in finding.message
    assert not [f for f in _findings(_facts()) if f.kind == "invented_text"]


def test_read_shots_keeps_what_it_read_by_take_and_reads_a_kept_take_from_the_cache(monkeypatch):
    from app.services.video_studio import render_cache

    kept: dict = {}
    calls: list = []

    async def get_json(kind, digest):
        return kept.get((kind, digest))

    async def put_json(kind, digest, value):
        kept[(kind, digest)] = value

    async def read_take(user_id, clip_id, take):
        calls.append(clip_id)
        return text_backfill.Backfill(True, (text_drift.Invented("Brovantek", "last_frame", SIGN, CROP),))

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(text_backfill, "read_take", read_take)
    takes = {SHOT: text_backfill.Unchecked(TAKE, 4.0, ("Filigran",))}
    first = asyncio.run(text_backfill.read_shots("u1", takes))
    second = asyncio.run(text_backfill.read_shots("u1", takes))
    assert calls == [SHOT] and first == second
    assert second[SHOT].invented == (text_drift.Invented("Brovantek", "last_frame", SIGN, CROP),)


def test_read_shots_bounds_the_takes_read_fresh(monkeypatch):
    from app.services.video_studio import render_cache

    async def nothing(*_a, **_k):
        return None

    async def read_take(_user, clip_id, _take):
        return text_backfill.Backfill(True)

    monkeypatch.setattr(render_cache, "get_json", nothing)
    monkeypatch.setattr(render_cache, "put_json", nothing)
    monkeypatch.setattr(text_backfill, "read_take", read_take)
    takes = {f"clip-{i}": text_backfill.Unchecked(f"file-{i}") for i in range(text_backfill.MAX_SHOTS_READ + 2)}
    found = asyncio.run(text_backfill.read_shots("u1", takes))
    assert len(found) == text_backfill.MAX_SHOTS_READ
    # The rest is said not run, read by the next check.
    assert text_backfill.fact_line("clip-9", None).startswith("Text check not run on clip `clip-9`")


def test_backfill_round_trips_and_an_unread_result_is_never_cached():
    item = text_drift.Invented("Brovantek", "middle_frame", SIGN, CROP)
    read = text_backfill.Backfill(True, (item,))
    assert text_backfill.Backfill.from_dict(read.as_dict()) == read
    assert text_backfill.Backfill.from_dict({"read": False, "why": "x"}) is None
    assert text_backfill.Backfill.from_dict(SimpleNamespace()) is None


def test_a_read_slower_than_the_budget_is_never_cut_and_the_next_check_answers_it(monkeypatch):
    """Issue #9021: a save check's budget used to cancel the read, so a slow take was 'read by the next one' forever."""
    from app.services.video_studio import render_cache

    calls: list = []

    async def nothing(*_a, **_k):
        return None

    async def slow(_user, clip_id, _take):
        calls.append(clip_id)
        await asyncio.sleep(0.2)
        return text_backfill.Backfill(True, (text_drift.Invented("Mosoura", "middle_frame", SIGN, CROP),))

    monkeypatch.setattr(render_cache, "get_json", nothing)
    monkeypatch.setattr(render_cache, "put_json", nothing)
    monkeypatch.setattr(text_backfill, "read_take", slow)
    takes = {SHOT: text_backfill.Unchecked(TAKE, 6.0)}

    async def three_saves():
        first = await text_backfill.read_shots("u1", takes, timeout=0.01)
        second = await text_backfill.read_shots("u1", takes, timeout=0.01)  # joins the read still running
        await asyncio.sleep(0.3)
        third = await text_backfill.read_shots("u1", takes, timeout=0.01)
        return first, second, third

    first, second, third = asyncio.run(three_saves())
    assert first == {} and second == {} and calls == [SHOT]
    assert third[SHOT].read and third[SHOT].invented[0].word == "Mosoura"


def test_a_read_that_fails_is_said_with_its_reason_not_deferred(monkeypatch):
    from app.services.video_studio import render_cache

    async def nothing(*_a, **_k):
        return None

    async def broken(*_a):
        raise RuntimeError("no chat model")

    monkeypatch.setattr(render_cache, "get_json", nothing)
    monkeypatch.setattr(render_cache, "put_json", nothing)
    monkeypatch.setattr(text_backfill, "read_take", broken)
    found = asyncio.run(text_backfill.read_shots("u1", {SHOT: text_backfill.Unchecked(TAKE)}))
    assert not found[SHOT].read and "no chat model" in text_backfill.fact_line(SHOT, found[SHOT])


def _document(*scenes: tuple[str, float, float]) -> dict:
    return {"scenes": [{"id": sid, "start": start, "end": end} for sid, start, end in scenes]}


def test_a_render_files_invented_text_with_its_crop_on_each_scene_playing_the_take():
    from app.services.video_studio import render_checks

    word = text_drift.Invented("Macana", "first_frame", SIGN, CROP)

    async def read(user_id, takes, *, timeout, limit):
        assert set(takes) == {SHOT} and limit == 1
        return {SHOT: text_backfill.Backfill(True, (word,))}

    found, lines = asyncio.run(
        text_backfill.render_findings(
            "u1",
            _document(("lab-moscone", 0.0, 6.0), ("card", 6.0, 9.0)),
            {"lab-moscone": SHOT},
            {SHOT: (text_backfill.Unchecked(TAKE, 6.0), True)},
            read=read,
        )
    )
    [(finding, crop)] = found
    assert finding.kind == render_checks.INVENTED_TEXT and finding.severity == render_checks.FAIL
    assert finding.scene_id == "lab-moscone" and (finding.at, finding.end) == (0.0, 6.0) and crop == CROP
    assert '"Macana" (first frame' in finding.message and f"clip_id `{SHOT}`" in finding.fix
    assert render_checks.INVENTED_TEXT in render_checks.PICTURE_KINDS
    assert lines == [text_backfill.fact_line(SHOT, text_backfill.Backfill(True, (word,)))]


def test_a_render_warns_on_a_shot_asked_with_text_check_false_and_says_a_clean_read():
    from app.services.video_studio import render_checks

    other = "00000000-0000-0000-0000-00000000c002"

    async def read(_user, _takes, **_kw):
        return {
            SHOT: text_backfill.Backfill(True, (text_drift.Invented("Codtin", "last_frame", SIGN),)),
            other: text_backfill.Backfill(True),
        }

    found, lines = asyncio.run(
        text_backfill.render_findings(
            "u1",
            _document(("a", 0.0, 4.0), ("b", 4.0, 8.0)),
            {"a": SHOT, "b": other},
            {SHOT: (text_backfill.Unchecked(TAKE), False), other: (text_backfill.Unchecked("f2"), True)},
            read=read,
        )
    )
    [(finding, crop)] = found
    assert finding.severity == render_checks.WARN and crop is None
    assert lines[1].endswith("first, middle and last frames, no invented lettering.")


def test_a_scene_preview_reads_its_own_scene_only_and_says_a_read_still_running():
    asked: list = []

    async def read(_user, takes, **_kw):
        asked.append(set(takes))
        return {}

    other = "00000000-0000-0000-0000-00000000c002"
    found, lines = asyncio.run(
        text_backfill.render_findings(
            "u1",
            _document(("a", 0.0, 4.0), ("b", 4.0, 8.0)),
            {"a": SHOT, "b": other},
            {SHOT: (text_backfill.Unchecked(TAKE), True), other: (text_backfill.Unchecked("f2"), True)},
            scene_id="b",
            span=(4.0, 4.0),
            offset=4.0,
            read=read,
        )
    )
    assert asked == [{other}] and found == []
    assert lines == [text_backfill.fact_line(other, None)] and "still being read" in lines[0]


def test_a_render_with_no_unchecked_take_reads_nothing():
    async def read(*_a, **_k):
        raise AssertionError("nothing to read")

    found = asyncio.run(text_backfill.render_findings("u1", _document(("a", 0.0, 4.0)), {"a": SHOT}, {}, read=read))
    assert found == ([], [])
