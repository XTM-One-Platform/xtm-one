"""Live audience polls (xtm-one #10157): the block model and the pure parts of the service.

The database side (codes, votes, one vote per device, lock, reset) is covered
in ``test_presentation_polls_integration.py`` against a real PostgreSQL.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.presentation_polls import (
    BLOCKED_WORDS,
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_RE,
    PollError,
    PollNoopError,
    _new_code,
    _question_results,
    _vote_values,
    cast_vote,
    clean_words,
    control_poll,
    opens_every_question,
    poll_qr_svg,
    question_is_open,
    spec_with_poll_mode,
)
from app.services.presentation_spec import BLOCK_TYPES, AtomicBlock, PollBlock, poll_blocks

ATOMIC = TypeAdapter(AtomicBlock)

QUIZ = {"id": "q1", "kind": "quiz", "prompt": "2 + 2?", "options": ["3", "4", "5"], "correct": 1}
CHOICE = {"id": "q2", "kind": "choice", "prompt": "Favourite colour", "options": ["red", "blue"]}
CLOUD = {"id": "q3", "kind": "word_cloud", "prompt": "One word"}


def _poll(*questions: dict) -> dict:
    return {"type": "poll", "id": "kickoff", "questions": list(questions)}


def test_poll_is_in_the_catalog_and_validates_the_acceptance_deck():
    assert "poll" in BLOCK_TYPES
    block = ATOMIC.validate_python(_poll(QUIZ, CHOICE, CLOUD))
    assert isinstance(block, PollBlock)
    assert block.show_qr is True
    assert block.show_results == "live"
    assert [q.kind for q in block.questions] == ["quiz", "choice", "word_cloud"]
    assert block.layout == "active"


def test_a_full_slide_poll_shows_every_question_and_refuses_an_unknown_layout():
    block = ATOMIC.validate_python({**_poll(QUIZ, CHOICE, CLOUD), "layout": "all"})
    assert block.layout == "all"
    with pytest.raises(ValidationError):
        ATOMIC.validate_python({**_poll(QUIZ), "layout": "grid"})


@pytest.mark.parametrize(
    "question",
    [
        {"id": "q", "kind": "choice", "prompt": "One option", "options": ["only"]},
        {"id": "q", "kind": "quiz", "prompt": "No answer", "options": ["a", "b"]},
        {"id": "q", "kind": "quiz", "prompt": "Out of range", "options": ["a", "b"], "correct": 2},
        {"id": "q", "kind": "choice", "prompt": "Answer on a choice", "options": ["a", "b"], "correct": 0},
        {"id": "q", "kind": "word_cloud", "prompt": "Options on a cloud", "options": ["a", "b"]},
        {"id": "q", "kind": "choice", "prompt": "Seven", "options": list("abcdefg")},
    ],
)
def test_a_question_whose_shape_does_not_match_its_kind_is_refused(question):
    with pytest.raises(ValidationError):
        ATOMIC.validate_python(_poll(question))


def test_question_ids_are_unique_and_at_most_five_questions():
    with pytest.raises(ValidationError):
        ATOMIC.validate_python(_poll(QUIZ, {**CHOICE, "id": "q1"}))
    with pytest.raises(ValidationError):
        ATOMIC.validate_python(_poll(*[{**CHOICE, "id": f"q{i}"} for i in range(6)]))


def test_poll_blocks_finds_polls_nested_in_columns():
    spec = {"slides": [{"blocks": [{"type": "columns", "columns": [[_poll(QUIZ)], []]}]}, {"blocks": []}]}
    assert [b["id"] for b in poll_blocks(spec)] == ["kickoff"]
    assert poll_blocks({"slides": []}) == []
    assert poll_blocks(None) == []


def test_codes_are_short_and_unambiguous():
    for _ in range(50):
        code = _new_code()
        assert len(code) == CODE_LENGTH
        assert CODE_RE.fullmatch(code)
    assert not set("01ILO") & set(CODE_ALPHABET)


def test_word_cloud_answers_are_cleaned_bounded_and_filtered():
    assert clean_words("  Speed, speed ; Trust ", 3) == ["speed", "trust"]
    assert clean_words("one, two, three", 1) == ["one"]
    with pytest.raises(PollError) as refused:
        clean_words(f"nice {sorted(BLOCKED_WORDS)[0]}", 1)
    assert refused.value.reason == "filtered"
    with pytest.raises(PollError):
        clean_words(" , ;", 1)


def test_vote_values_check_the_answer_against_the_question():
    assert _vote_values(QUIZ, 1) == ["1"]
    assert _vote_values({"id": "r", "kind": "rating", "prompt": "Rate"}, 5) == ["5"]
    assert _vote_values(CLOUD, "Fast") == ["fast"]
    for bad in (3, -1, True, "1"):
        with pytest.raises(PollError):
            _vote_values(QUIZ, bad)
    with pytest.raises(PollError):
        _vote_values({"id": "r", "kind": "rating", "prompt": "Rate"}, 6)


def test_results_count_per_option_rating_and_words():
    quiz = _question_results(QUIZ, ["1", "1", "0"])
    assert quiz["counts"] == [1, 2, 0] and quiz["total"] == 3
    rating = _question_results({"id": "r", "kind": "rating", "prompt": "Rate"}, ["5", "3", "4"])
    assert rating["counts"] == [0, 0, 1, 1, 1] and rating["average"] == 4.0
    cloud = _question_results(CLOUD, ["fast", "fast", "safe"])
    assert cloud["words"][0] == {"text": "fast", "count": 2}


class _Db:
    """A session double for the vote path: every count reads 0, every insert records a row."""

    def __init__(self) -> None:
        self.inserts = 0

    async def execute(self, stmt):
        sql = str(stmt).lower()
        db = self

        class _Result:
            def scalar_one(self):
                return 0

            def scalar_one_or_none(self):
                return None

            def scalar(self):
                if sql.startswith("insert"):
                    db.inserts += 1
                    return "row"
                return None

        return _Result()


def _ctx(layout: str, active: str = "q1"):
    from types import SimpleNamespace

    block = {**_poll(QUIZ, CHOICE, CLOUD), "layout": layout}
    poll = SimpleNamespace(id="p", locked=False, active_question=active)
    return SimpleNamespace(poll=poll, block=block)


TOKEN = "a" * 32


def test_layout_all_opens_every_question_and_active_only_the_current_one():
    every = {**_poll(QUIZ, CHOICE, CLOUD), "layout": "all"}
    stepped = _poll(QUIZ, CHOICE, CLOUD)
    assert opens_every_question(every) and not opens_every_question(stepped)
    assert all(question_is_open(every, "q1", key) for key in ("q1", "q2", "q3"))
    assert question_is_open(stepped, "q1", "q1")
    assert not question_is_open(stepped, "q1", "q3")
    assert not question_is_open(every, "q1", "nope")


async def test_a_vote_on_a_non_active_question_is_accepted_when_layout_is_all():
    db = _Db()
    assert await cast_vote(db, _ctx("all"), question_key="q2", voter_token=TOKEN, answer=1) is True
    assert await cast_vote(db, _ctx("all"), question_key="q3", voter_token=TOKEN, answer="fast") is True
    assert db.inserts == 2


async def test_a_vote_on_a_non_active_question_is_refused_when_layout_is_active():
    db = _Db()
    with pytest.raises(PollError) as refused:
        await cast_vote(db, _ctx("active"), question_key="q2", voter_token=TOKEN, answer=1)
    assert refused.value.reason == "not_open"
    assert db.inserts == 0
    assert await cast_vote(db, _ctx("active"), question_key="q1", voter_token=TOKEN, answer=1) is True


async def test_a_locked_all_poll_refuses_every_question():
    ctx = _ctx("all")
    ctx.poll.locked = True
    with pytest.raises(PollError) as refused:
        await cast_vote(_Db(), ctx, question_key="q2", voter_token=TOKEN, answer=1)
    assert refused.value.reason == "locked"


@pytest.mark.parametrize("action", ["open", "next"])
async def test_stepping_an_all_poll_is_a_noop(monkeypatch, action):
    from types import SimpleNamespace

    import app.services.presentation_polls as polls

    block = {**_poll(QUIZ, CHOICE, CLOUD), "layout": "all"}
    row = SimpleNamespace(id="p", locked=False, revealed=False, active_question="q1")

    async def _rows(_db, _presentation):
        return {"kickoff": row}

    monkeypatch.setattr(polls, "ensure_polls", _rows)
    presentation = SimpleNamespace(spec={"slides": [{"blocks": [block]}]})
    with pytest.raises(PollNoopError):
        await control_poll(_Db(), presentation, "kickoff", action, "q2")
    assert row.active_question == "q1"


@pytest.mark.parametrize(("mode", "layout"), [("guided", "active"), ("autonomous", "all")])
def test_a_poll_mode_is_its_layout(mode, layout):
    block = ATOMIC.validate_python({**_poll(QUIZ, CHOICE), "mode": mode})
    assert block.layout == layout
    assert "mode" not in block.model_dump()
    with pytest.raises(ValidationError):
        ATOMIC.validate_python({**_poll(QUIZ), "mode": "freestyle"})


def test_switching_a_poll_mode_rewrites_only_its_layout():
    spec = {"slides": [{"blocks": [_poll(QUIZ, CHOICE, CLOUD)]}]}
    autonomous = spec_with_poll_mode(spec, "kickoff", "autonomous")
    assert autonomous is not None
    assert autonomous["slides"][0]["blocks"][0]["layout"] == "all"
    # The poll keeps its id (votes hang on it) and the source spec is untouched.
    assert autonomous["slides"][0]["blocks"][0]["id"] == "kickoff"
    assert "layout" not in spec["slides"][0]["blocks"][0]
    assert spec_with_poll_mode(autonomous, "kickoff", "autonomous") is None
    guided = spec_with_poll_mode(autonomous, "kickoff", "guided")
    assert guided is not None and guided["slides"][0]["blocks"][0]["layout"] == "active"
    with pytest.raises(ValueError):
        spec_with_poll_mode(spec, "nope", "guided")
    with pytest.raises(ValueError):
        spec_with_poll_mode(spec, "kickoff", "freestyle")


def test_a_hands_free_poll_validates_and_defaults_to_today():
    block = ATOMIC.validate_python(_poll(QUIZ))
    assert (block.controls, block.reveal) == ("hover", "manual")
    hands_free = ATOMIC.validate_python({**_poll(QUIZ, CHOICE), "layout": "all", "controls": "none", "reveal": "live"})
    assert (hands_free.controls, hands_free.reveal) == ("none", "live")
    with pytest.raises(ValidationError):
        ATOMIC.validate_python({**_poll(QUIZ), "controls": "bar"})
    with pytest.raises(ValidationError):
        ATOMIC.validate_python({**_poll(QUIZ), "reveal": "later"})


@pytest.mark.parametrize(("reveal", "shown"), [("live", True), ("manual", False)])
async def test_reveal_live_marks_the_quiz_answer_from_the_start(monkeypatch, reveal, shown):
    from types import SimpleNamespace

    import app.services.presentation_polls as polls

    async def _no_counts(_db, _poll_id):
        return {}

    class _Voters:
        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: [])

    monkeypatch.setattr(polls, "_counts", _no_counts)
    block = {**_poll(QUIZ, CHOICE), "layout": "all", "reveal": reveal}
    row = SimpleNamespace(id="p", code="ABCDE", locked=False, revealed=False, active_question=None)
    state = await polls.poll_state(_Voters(), row, block, audience=True)
    assert state["revealed"] is shown
    quiz = next(q for q in state["questions"] if q["id"] == "q1")
    assert ("correct" in quiz) is shown


def test_the_reveal_conditions_validate_against_their_mode():
    block = ATOMIC.validate_python({**_poll(QUIZ), "controls": "none", "reveal": "after_votes", "reveal_after_votes": 3})
    assert (block.reveal, block.reveal_after_votes) == ("after_votes", 3)
    timed = ATOMIC.validate_python({**_poll(QUIZ), "reveal": "after_seconds"})
    assert timed.reveal_after_seconds is None
    assert ATOMIC.validate_python({**_poll(QUIZ), "reveal": "never"}).reveal == "never"
    # after_votes without a count reads the default (UX round 1 B2: picking the mode saves).
    assert ATOMIC.validate_python({**_poll(QUIZ), "reveal": "after_votes"}).reveal_after_votes is None
    for wrong in ({"reveal": "after_votes", "reveal_after_votes": 0}, {"reveal": "after_seconds", "reveal_after_seconds": 0}):
        with pytest.raises(ValidationError):
            ATOMIC.validate_python({**_poll(QUIZ), **wrong})


@pytest.mark.parametrize(
    ("change", "kept"),
    [
        ({"reveal": "after_seconds", "reveal_after_votes": 3}, {"reveal_after_votes": None, "reveal_after_seconds": None}),
        ({"reveal": "after_votes", "reveal_after_votes": 3, "reveal_after_seconds": 10}, {"reveal_after_votes": 3, "reveal_after_seconds": None}),
        ({"reveal": "live", "reveal_after_votes": 3, "reveal_after_seconds": 10}, {"reveal_after_votes": None, "reveal_after_seconds": None}),
        ({"reveal_after_seconds": 20}, {"reveal_after_votes": None, "reveal_after_seconds": None}),
    ],
)
def test_a_mode_change_drops_the_number_the_new_mode_does_not_read(change, kept):
    """UX round 1 B2 of #10417: switching after_votes 3 to after_seconds saves, the count dropped."""
    block = ATOMIC.validate_python({**_poll(QUIZ), **change})
    assert {"reveal_after_votes": block.reveal_after_votes, "reveal_after_seconds": block.reveal_after_seconds} == kept


def test_question_revealed_follows_each_mode():
    from datetime import UTC, datetime, timedelta

    from app.services.presentation_polls import question_revealed

    now = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)
    votes3 = {"reveal": "after_votes", "reveal_after_votes": 3}
    default_votes = {"reveal": "after_votes"}
    assert question_revealed(default_votes, flag=False, votes=9, first_vote_at=now, now=now) is False
    assert question_revealed(default_votes, flag=False, votes=10, first_vote_at=now, now=now) is True
    assert question_revealed(votes3, flag=False, votes=0, first_vote_at=None, now=now) is False
    assert question_revealed(votes3, flag=False, votes=2, first_vote_at=now, now=now) is False
    assert question_revealed(votes3, flag=False, votes=3, first_vote_at=now, now=now) is True
    # The presenter's reveal shows a conditional answer early.
    assert question_revealed(votes3, flag=True, votes=0, first_vote_at=None, now=now) is True
    timed = {"reveal": "after_seconds"}
    assert question_revealed(timed, flag=False, votes=0, first_vote_at=None, now=now) is False
    assert question_revealed(timed, flag=False, votes=1, first_vote_at=now - timedelta(seconds=19), now=now) is False
    assert question_revealed(timed, flag=False, votes=1, first_vote_at=now - timedelta(seconds=20), now=now) is True
    short = {"reveal": "after_seconds", "reveal_after_seconds": 5}
    naive = (now - timedelta(seconds=6)).replace(tzinfo=None)
    assert question_revealed(short, flag=False, votes=1, first_vote_at=naive, now=now) is True
    never = {"reveal": "never"}
    assert question_revealed(never, flag=True, votes=99, first_vote_at=now, now=now) is False
    assert question_revealed({"reveal": "live"}, flag=False, votes=0, first_vote_at=None, now=now) is True
    assert question_revealed({}, flag=False, votes=5, first_vote_at=now, now=now) is False
    assert question_revealed({}, flag=True, votes=0, first_vote_at=None, now=now) is True


@pytest.mark.parametrize(("votes", "shown"), [(0, False), (2, False), (3, True)])
async def test_after_votes_hides_the_answer_from_the_audience_until_the_threshold(monkeypatch, votes, shown):
    from types import SimpleNamespace

    import app.services.presentation_polls as polls

    async def _counts(_db, _poll_id):
        return {"q1": ["1"] * votes} if votes else {}

    class _Voters:
        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: [f"v{i}" for i in range(votes)])

    monkeypatch.setattr(polls, "_counts", _counts)
    block = {**_poll(QUIZ, CHOICE), "layout": "all", "controls": "none", "reveal": "after_votes", "reveal_after_votes": 3}
    row = SimpleNamespace(id="p", code="ABCDE", locked=False, revealed=False, active_question=None)
    audience = await polls.poll_state(_Voters(), row, block, audience=True)
    quiz = next(q for q in audience["questions"] if q["id"] == "q1")
    assert quiz["revealed"] is shown
    assert ("correct" in quiz) is shown
    assert quiz["results"]["total"] == votes  # the bars fill live either way
    assert audience["revealed"] is False  # the presenter did not reveal
    slide = await polls.poll_state(_Voters(), row, block, audience=False)
    on_slide = next(q for q in slide["questions"] if q["id"] == "q1")
    assert on_slide["revealed"] is shown and on_slide["correct"] == 1
    # The condition alone, so the controls drop Reveal / Hide once it holds (UX round 1 B1 of #10417).
    assert on_slide["condition_met"] is shown
    flagged = await polls.poll_state(_Voters(), SimpleNamespace(**{**vars(row), "revealed": True}), block, audience=False)
    assert next(q for q in flagged["questions"] if q["id"] == "q1")["condition_met"] is shown
    assert "revealed" not in next(q for q in slide["questions"] if q["id"] == "q2")


def test_the_qr_is_an_svg_data_uri():
    assert poll_qr_svg("https://example.test/v/ABCDE").startswith("data:image/svg+xml;base64,")
