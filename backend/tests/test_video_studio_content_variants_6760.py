"""A plural subject described by variants is checked as several distinct subjects, none identical.

``generate_video_clip``'s content check (``content_drift.check``) read "five
cats, each a different colour: ginger, grey, white, calico" as a contract,
variant by variant, and failed a take showing several distinct cats with one
variant missing. On a stub chat model, no provider: the prompt's subject of
several - read as one, or read variant by variant and grouped by its kind in
code - is told to the frames reading with its count and its variants; five
distinct cats with no calico pass, two identical cats fail, and the negative
prompt or a subject of another kind still fail.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import content_drift, encoder

PROMPT = "five cats, each a different colour: ginger, grey, white, black, tabby, on a sofa"
VARIANTS = ["ginger", "grey", "white", "black", "tabby"]


@pytest.fixture
def reader(monkeypatch):
    """A chat model answering the prompt reading with *world.subjects* and the frames reading with *world.frames*."""
    world = SimpleNamespace(calls=[], subjects=[{"name": "cats", "kind": "cat", "count": 5, "variants": VARIANTS}], frames={})

    class _Llm:
        async def chat(self, messages, model=None, temperature=None, max_tokens=None):
            world.calls.append(messages[0].content)
            if not messages[0].files:
                return SimpleNamespace(content=json.dumps({"subjects": world.subjects, "camera": "locked", "camera_terms": []}))
            return SimpleNamespace(content=json.dumps({"frames": world.frames}))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def get_llm(_db, _user_id):
        return _Llm(), "chat-model"

    async def extract_still(_video, *, at, out_path, max_side, crop_to=None):
        Path(out_path).write_bytes(b"JPG")

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    return world


def _seen(name: str = "cats", *, count: int = 5, identical: bool = False, kind_matches: bool = True, seen_as: str = "", excluded=()) -> dict:
    """The frames reading: the subject on all three frames, *count* of it, *identical* or not."""
    subject = {"name": name, "present": True, "kind_matches": kind_matches, "seen_as": seen_as, "replaced_by": "", "count": count, "identical": identical, "anomalies": []}
    return {frame: {"subjects": [subject], "excluded_seen": list(excluded)} for frame in content_drift.FRAMES}


def _check(tmp_path, *, negative: str | None = None, params: dict | None = None) -> content_drift.ContentCheck:
    return asyncio.run(
        content_drift.check(
            "u", prompt=PROMPT, negative_prompt=negative, video_path="shot.mp4", duration=8.0, workdir=str(tmp_path), params=params if params is not None else {}
        )
    )


def test_five_distinct_cats_with_no_calico_pass_and_the_frames_reading_is_told_they_are_several(reader, tmp_path):
    reader.frames = _seen()
    verdict = _check(tmp_path)
    assert verdict.checked and verdict.findings == [] and verdict.failing() == []
    [subject] = verdict.subjects
    assert subject.several and subject.count == 5 and subject.variants == tuple(VARIANTS)
    asked = reader.calls[-1]
    assert "cats (cat; several, 5 of them, each a different one: ginger, grey, white, black, tabby)" in asked
    assert "whether two or more of them look the same (identical)" in asked and "does not matter" in asked
    # The prompt reading is asked a plural subject as one.
    assert "are one subject" in reader.calls[0] and '"variants": []' in reader.calls[0]


def test_two_identical_cats_fail(reader, tmp_path):
    reader.frames = _seen(count=2, identical=True)
    verdict = _check(tmp_path)
    failing = verdict.failing()
    # On each frame read that shows them.
    assert [f.frame for f in failing] == list(content_drift.FRAMES)
    assert {(f.severity, f.what) for f in failing} == {(content_drift.MAJOR, "cats: two or more look identical, where each is a different one")}
    assert "content drift" in verdict.reason()


def test_one_cat_alone_where_several_are_asked_fails(reader, tmp_path):
    reader.frames = _seen(count=1)
    [finding] = _check(tmp_path).failing()
    assert finding.what == "cats: only one shows, where several are asked"


def test_the_negative_prompt_or_a_subject_of_another_kind_still_fails(reader, tmp_path):
    reader.frames = _seen(excluded=[{"term": "dogs", "noticeable": True}])
    excluded = _check(tmp_path, negative="dogs").failing()
    assert excluded and all(f.excluded and f.what == "shows dogs, which the negative prompt excludes" for f in excluded)
    reader.frames = _seen(kind_matches=False, seen_as="a dog")
    other = _check(tmp_path).failing()
    assert other and {f.what for f in other} == {"cats shows as a dog, not cat"}


def test_variants_read_one_by_one_are_grouped_by_their_kind_and_no_missing_one_is_a_finding(reader, tmp_path):
    # The prompt reading lists each variant as a subject: grouped in code, never a contract one by one.
    reader.subjects = [{"name": f"{variant} cat", "kind": "cat"} for variant in [*VARIANTS, "calico"]]
    reader.frames = _seen(name="cat")
    params: dict = {}
    verdict = _check(tmp_path, params=params)
    assert verdict.findings == []
    [subject] = verdict.subjects
    assert subject.name == "cat" and subject.several and "calico cat" in subject.variants
    assert "never shows" not in json.dumps([f.what for f in verdict.findings])
    # Kept with the shot as read now ...
    assert params[content_drift.SUBJECTS_KEY]["subjects"][0]["variants"][-1] == "calico cat"
    # ... and a reading kept before, variant by variant, grouped the same when the shot is checked again.
    kept = {"digest": content_drift.prompt_digest(PROMPT, None), "camera": "locked", "camera_terms": []}
    kept["subjects"] = [{"name": f"{variant} cat", "kind": "cat"} for variant in VARIANTS]
    calls = len(reader.calls)
    again = _check(tmp_path, params={content_drift.SUBJECTS_KEY: kept})
    assert again.findings == [] and len(reader.calls) == calls + 1, "the frames read, the prompt never again"


def test_subjects_of_different_kinds_stay_apart_and_one_never_shown_still_fails():
    subjects = content_drift.grouped([content_drift.Subject("a cat", "cat"), content_drift.Subject("a dog", "dog")])
    assert [s.name for s in subjects] == ["a cat", "a dog"] and not any(s.several for s in subjects)
    answer = {"frames": {frame: {"subjects": [{"name": "a cat", "present": True, "kind_matches": True}, {"name": "a dog", "present": False}]} for frame in content_drift.FRAMES}}
    [finding] = content_drift.drifted(subjects, [], answer)
    assert finding.what == "a dog never shows"
