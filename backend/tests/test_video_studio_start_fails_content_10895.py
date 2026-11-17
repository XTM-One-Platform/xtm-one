"""A start picture that already fails the content check is refused before anything is paid (issue #10895).

A shot started from a still of two look-alike black cats, its prompt asking
"two clearly different cats", passed ``validate_only`` and then failed every
paid take on its first frame ("two or more look identical"): the first frame
is the start picture. On a stub chat model, no provider: the start picture is
read against the prompt's subjects before anything is paid and refused with
``start_fails_content_check``; a picture that passes answers as before; a take's
first-frame finding the picture shares is said inherited from the start picture.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import clips, content_drift

PROMPT = "two clearly different cats sit side by side"
#: The reading under test, before the suite's autouse fixture stubs it (conftest ``_no_start_negative_reading``).
READ_START_CONTENT = content_drift.read_start_content


def _cats(identical: bool, count: int = 2) -> dict:
    cats = {
        "name": "cats",
        "present": True,
        "kind_matches": True,
        "seen_as": "",
        "replaced_by": "",
        "count": count,
        "identical": identical,
        "anomalies": [],
    }
    return {"frames": {"first_frame": {"subjects": [cats], "excluded_seen": []}}}


@pytest.fixture
def reader(monkeypatch):
    """A chat model answering the prompt reading and the start picture reading; the picture stored as file-1."""
    world = SimpleNamespace(calls=[], picture=_cats(identical=True))

    class _Llm:
        async def chat(self, messages, model=None, temperature=None, max_tokens=None):
            message = messages[0]
            world.calls.append(message.content)
            if not message.files:
                subjects = [{"name": "cats", "kind": "cat", "count": 2, "variants": []}]
                return SimpleNamespace(
                    content=json.dumps({"subjects": subjects, "camera": "locked", "camera_terms": []})
                )
            return SimpleNamespace(content=json.dumps(world.picture))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def get_llm(_db, _user_id):
        return _Llm(), "chat-model"

    async def download_file(_file_id, out_path):
        Path(out_path).write_bytes(b"PNG")
        return SimpleNamespace(content_type="image/png")

    async def start_picture(data, mime):
        return SimpleNamespace(data=data, mime=mime)

    monkeypatch.setattr("app.core.database.async_session", session)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr("app.services.video_studio.assets.download_file", download_file)
    monkeypatch.setattr(clips, "start_picture", start_picture)
    monkeypatch.setattr(content_drift, "read_start_content", READ_START_CONTENT)
    return world


def _lint(params: dict, prompt: str | None = PROMPT) -> None:
    asyncio.run(clips.lint_start_content(SimpleNamespace(id="u"), params, prompt))


def test_look_alike_subjects_on_the_start_picture_are_refused_before_payment(reader):
    params = {"reference_file_id": "file-1"}
    with pytest.raises(clips.ClipError) as refused:
        _lint(params)
    said = str(refused.value)
    assert said.startswith(content_drift.START_FAILS_CODE)
    assert "two or more look identical" in said
    assert "another picture" in said and "reference images" in said and "content_check: false" in said
    assert refused.value.unpaid
    # The reading is kept with the shot, and the prompt's subjects with it for the take's check.
    kept = content_drift.kept_start_findings(params, content_drift.start_content_digest(PROMPT, None, "file-1"))
    assert kept and kept[0]["subject"] == "cats"
    assert params[content_drift.SUBJECTS_KEY]["subjects"][0]["name"] == "cats"


def test_a_start_picture_that_passes_answers_as_before(reader):
    reader.picture = _cats(identical=False)
    params = {"reference_file_id": "file-1"}
    _lint(params)
    assert params[content_drift.START_FINDINGS_KEY]["findings"] == []
    # Kept for the same picture and prompt: not read again.
    calls = len(reader.calls)
    _lint(params)
    assert len(reader.calls) == calls


def test_only_one_subject_on_the_start_picture_is_never_refused(reader):
    # One cat now, the second may walk in: only the take's frames can tell.
    reader.picture = _cats(identical=False, count=1)
    _lint({"reference_file_id": "file-1"})


def test_no_picture_no_prompt_or_no_content_check_reads_nothing(reader):
    _lint({})
    _lint({"reference_file_id": "file-1"}, prompt="  ")
    off = {"reference_file_id": "file-1", "content_check": False, content_drift.START_FINDINGS_KEY: {"digest": "x"}}
    _lint(off)
    assert content_drift.START_FINDINGS_KEY not in off
    assert reader.calls == []


def test_an_unusable_reading_is_never_refused(reader):
    reader.picture = {"nothing": True}
    params = {"reference_file_id": "file-1"}
    _lint(params)
    assert content_drift.START_FINDINGS_KEY not in params


def test_another_prompt_or_picture_is_read_anew():
    digest = content_drift.start_content_digest(PROMPT, None, "file-1")
    params = {content_drift.START_FINDINGS_KEY: {"digest": digest, "findings": []}}
    assert content_drift.kept_start_findings(params, digest) == []
    assert (
        content_drift.kept_start_findings(params, content_drift.start_content_digest("one cat", None, "file-1")) is None
    )
    assert content_drift.kept_start_findings(params, content_drift.start_content_digest(PROMPT, None, "file-2")) is None


def test_a_take_failure_the_start_picture_shares_is_said_inherited():
    subject = content_drift.Subject("cats", "cat", 2)
    take = content_drift.drifted([subject], [], _cats(identical=True))
    start = content_drift.first_frame_failures(take)
    verdict = content_drift.ContentCheck(True, take, start_findings=start)
    reason = verdict.reason()
    assert "first frame, major, inherited from the start picture" in reason
    assert "start from another picture" in reason
    # Without the start reading, the same failure reads as before.
    assert "inherited" not in content_drift.ContentCheck(True, take).reason()


def test_a_later_frame_or_another_finding_is_never_inherited():
    start = [{"subject": "cats", "what": "cats: two or more look identical, where each is a different one"}]
    middle = content_drift.Finding(
        "cats", "middle_frame", "cats: two or more look identical, where each is a different one"
    )
    other = content_drift.Finding("cats", "first_frame", "cats with an extra leg")
    assert not content_drift.inherited_from_start(middle, start)
    assert not content_drift.inherited_from_start(other, start)
