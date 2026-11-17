"""get_video(fields=["feedback"]): the studio requests made on a video, readable by an agent (#10481).

Synthetic messages only: the scene the playhead was on is stored on the request, the revisions the
answering turn saved are read from its stored tool trace, the answer is bounded, wrapped, oldest first.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.video_studio import studio_feedback as fb

VIDEO = "00000000-0000-0000-0000-0000000000b1"
OTHER = "00000000-0000-0000-0000-0000000000b2"


def _saved(video: str, revision: int, *, name: str = "edit_video", success: bool = True) -> dict:
    return {
        "name": name,
        "input": "{}",
        "output": f"Saved video **Demo** (`{video}`), revision {revision}, about 40 s.",
        "success": success,
    }


def test_the_scene_on_screen_is_kept_on_the_request_with_the_rest_of_its_metadata():
    stored = fb.with_studio_scene(json.dumps({"attachments": [{"file_id": "f1"}]}), " s3 ")
    assert json.loads(stored or "") == {"attachments": [{"file_id": "f1"}], "studio_scene": "s3"}
    assert json.loads(fb.with_studio_scene(None, "intro") or "") == {"studio_scene": "intro"}
    # No scene sent (or not a string): the metadata is left as it was.
    assert fb.with_studio_scene(None, None) is None
    assert fb.with_studio_scene('{"a": 1}', 3) == '{"a": 1}'
    assert len(json.loads(fb.with_studio_scene(None, "x" * 500) or "")["studio_scene"]) == 120


def test_the_revisions_a_reply_saved_are_read_from_its_trace_for_that_video_only():
    meta = {
        "tool_call_trace": [
            _saved(VIDEO, 7),
            _saved(OTHER, 3),
            _saved(VIDEO, 8, success=False),
            _saved(VIDEO, 9, name="get_video"),
            _saved(VIDEO, 7, name="create_video"),
            _saved(VIDEO, 10),
        ]
    }
    assert fb.saved_revisions(meta, VIDEO) == [7, 10]
    assert fb.saved_revisions({}, VIDEO) == []
    assert fb.saved_revisions({"tool_call_trace": "not a list"}, VIDEO) == []


def _request(minutes: int, scene: str | None, text: str, revisions: list[int]) -> fb._Request:
    at = datetime(2026, 11, 9, 18, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return fb._Request("c1", at, scene, text, list(revisions))


def _answer(monkeypatch, requests: list[fb._Request], conversations: int = 1, scenes=None) -> str:
    async def fake(_db, _user, _project):
        return list(requests), conversations

    monkeypatch.setattr(fb, "_requests", fake)
    project = SimpleNamespace(id=VIDEO)
    return asyncio.run(fb.studio_feedback(None, SimpleNamespace(id="u1"), project, scenes=scenes))


def test_each_request_is_listed_verbatim_with_its_scene_time_and_saved_revision(monkeypatch):
    text = _answer(
        monkeypatch,
        [
            _request(0, "s2", "The logo on scene 2 is cut at the top.", [5]),
            _request(5, "s4", "Narration too fast here;\nslow it down.", []),
            _request(9, None, "Render it.", []),
        ],
    )
    assert "3 request(s) in 1 conversation(s), oldest first." in text
    assert "UNTRUSTED" in text and "Video Studio conversations" in text
    assert (
        "- 2026-11-09 18:00 UTC, scene s2, conversation `c1`, saved revision 5:\n  The logo on scene 2 is cut at the top."
        in text
    )
    assert "scene s4, conversation `c1`, saved no revision:\n  Narration too fast here;\n  slow it down." in text
    assert "scene not recorded" in text
    assert text.index("The logo") < text.index("Narration") < text.index("Render it.")


def test_scenes_keep_only_the_requests_made_on_those_scenes(monkeypatch):
    text = _answer(
        monkeypatch,
        [_request(0, "s2", "alpha remark", []), _request(1, "s4", "beta remark", [])],
        scenes={"s4"},
    )
    assert "on scene s4: 1 request(s)" in text and "beta remark" in text and "alpha remark" not in text


def test_no_request_and_no_conversation_are_said(monkeypatch):
    assert "none - no studio conversation of yours on it." in _answer(monkeypatch, [], conversations=0)
    assert _answer(monkeypatch, [], conversations=2).endswith(": none.")


def test_a_long_history_keeps_the_newest_requests_and_names_the_rest(monkeypatch):
    many = [_request(i, "s1", f"request {i} " + "x" * 1500, []) for i in range(20)]
    text = _answer(monkeypatch, many)
    assert "request 19" in text and "request 0 " not in text
    assert "oldest are not shown (read_conversation_history reads them whole)" in text
    assert len(text) < fb.FEEDBACK_CHARS + 2_000
