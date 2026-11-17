"""restore_take and accept_attempt take an AI shot id given by its 8+ hex prefix, as a regeneration does.

* a prefix of one own shot reaches the same checks as its full id (the shot read with the full id);
* a prefix of several shots is refused naming them, before any shot is read;
* a prefix of nothing answers the usual "no AI shot" refusal;
* a full id is read as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import audit
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import clips
from app.tools.base import ToolCall

USER_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
FULL = "48b4e177-0b0c-4d1e-9f00-1234567890ab"
OTHER = "48b4e177-ffff-4d1e-9f00-1234567890ab"
TAKE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _user():
    return SimpleNamespace(id=USER_ID, role="user")


class _Session:
    """One shot of the caller, keyed by its full id; the ids read recorded."""

    def __init__(self, seen: dict):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, cid, **_kw):
        self.seen.setdefault("read", []).append(str(cid))
        if str(cid) == FULL:
            return SimpleNamespace(id=uuid.UUID(FULL), user_id=USER_ID, prompt="Lobby", params={})
        return None

    async def commit(self):
        self.seen["committed"] = True


@pytest.fixture
def clip_tool(monkeypatch):
    """``generate_video_clip`` of a signed-in caller, the prefix lookup answering *prefixes*."""
    seen: dict = {}
    prefixes: dict = {}

    async def find_user(_db, _user_id):
        return _user()

    async def resolve(_db, clip_id, _user):
        return prefixes.get(clip_id, (clip_id, None))

    def promote(clip, file_id):
        seen["promoted"] = (str(clip.id), file_id)
        return {"file_id": file_id, "params": {clips.ACCEPTED_ATTEMPT: {}}}, []

    def restore(clip, file_id):
        seen["restored"] = (str(clip.id), file_id)
        return {"file_id": file_id}, []

    async def log(*_a, **_kw):
        return None

    monkeypatch.setattr(tool, "async_session", lambda: _Session(seen))
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(tool, "_resolve_clip_id_prefix", resolve)
    monkeypatch.setattr(clips, "promote_attempt", promote)
    monkeypatch.setattr(clips, "restore_previous_take", restore)
    monkeypatch.setattr(audit, "log_audit", log)
    _definitions, executor = tool.build_video_studio_tools(None, str(USER_ID))

    async def call(**arguments):
        return await executor.execute_tool(ToolCall(id="c1", name="generate_video_clip", arguments=arguments))

    return call, seen, prefixes


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "done"), [("restore_take", "restored"), ("accept_attempt", "promoted")])
async def test_a_prefix_of_one_own_shot_reaches_the_same_checks_as_its_full_id(clip_tool, mode, done):
    call, seen, prefixes = clip_tool
    prefixes["48b4e177"] = (FULL, None)
    result = await call(clip_id="48b4e177", **{mode: TAKE})
    assert not result.is_error, result.content
    assert seen[done] == (FULL, TAKE)
    assert seen["read"] == [FULL]
    assert FULL in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["restore_take", "accept_attempt"])
async def test_a_prefix_of_several_shots_is_refused_naming_them_before_any_read(clip_tool, mode):
    call, seen, prefixes = clip_tool
    prefixes["48b4e177"] = ("48b4e177", f"Error: `48b4e177` starts several AI shots: {FULL}, {OTHER}.")
    result = await call(clip_id="48b4e177", **{mode: TAKE})
    assert '"invalid_parameters"' in result.content
    assert FULL in result.content and OTHER in result.content
    assert "read" not in seen and "committed" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["restore_take", "accept_attempt"])
async def test_a_prefix_of_nothing_answers_no_ai_shot(clip_tool, mode):
    call, seen, _prefixes = clip_tool
    result = await call(clip_id="deadbeef", **{mode: TAKE})
    assert '"invalid_parameters"' in result.content
    assert "no AI shot `deadbeef` of yours" in result.content
    assert "committed" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "done"), [("restore_take", "restored"), ("accept_attempt", "promoted")])
async def test_a_full_id_is_read_as_before(clip_tool, mode, done):
    call, seen, _prefixes = clip_tool
    result = await call(clip_id=FULL, **{mode: TAKE})
    assert not result.is_error, result.content
    assert seen[done] == (FULL, TAKE)
    assert seen["read"] == [FULL]
