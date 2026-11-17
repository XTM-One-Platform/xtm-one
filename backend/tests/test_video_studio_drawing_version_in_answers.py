"""Render and preview answers name the screen inserts' drawing version beside the quality report version
(dev:video-drawing-version-in-answers). Fakes only.

* stored checks record ``drawing_version`` (``screen_insert.DRAWING_VERSION``), kept when read back;
* a render's answer (``render_answer.of_render``) carries ``quality_report_version``, ``drawing_version``,
  ``live_drawing_version`` and ``build`` in its dict and ends its text with them;
* a render drawn at ``DRAWING_VERSION - 1`` is told to render again, in its answer and in ``get_video``'s list;
* checks stored before ``drawing_version`` fall back on the whole render's ``drawing.release``;
* ``get_video`` states the live build's drawing version above its render list.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.core.config import settings
from app.models.video_studio import JOB_READY, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, quality, render_answer, render_checks, screen_insert

LIVE = screen_insert.DRAWING_VERSION


def _checks(monkeypatch, **values) -> dict:
    monkeypatch.setattr(settings, "app_version", "1.261106.0-dev.abc1234")
    return {**render_checks.Checks().as_dict(), **values}


def _render(**values) -> VideoRender:
    fields = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": JOB_READY,
        "quality": "standard",
        "revision": 2,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "duration_ms": 10_000,
        "file_id": None,
        "scene_id": None,
        "keep": False,
    }
    return VideoRender(**{**fields, **values})


def test_stored_checks_record_the_drawing_version_and_keep_it(monkeypatch):
    stored = _checks(monkeypatch)
    assert stored["drawing_version"] == LIVE
    old = {**stored, "drawing_version": LIVE - 1}
    assert render_checks.Checks.from_dict(old).as_dict()["drawing_version"] == LIVE - 1


def test_a_preview_answer_shows_its_drawing_version_beside_the_report_version(monkeypatch):
    preview = SimpleNamespace(status=JOB_READY, scene_id="s01", notes=[], drawing=None)
    answer = render_answer.of_render(preview, _checks(monkeypatch))
    out = answer.as_dict()
    assert out["quality_report_version"] == quality.QUALITY_REPORT_VERSION
    assert out["drawing_version"] == LIVE and out["live_drawing_version"] == LIVE
    assert out["build"] == "1.261106.0-dev.abc1234"
    assert answer.compact_dict()["drawing_version"] == LIVE
    text = answer.text()
    assert f"drawn at screen-insert drawing {LIVE} (live {LIVE})" in text
    assert "render again" not in text


def test_a_render_drawn_at_an_older_drawing_is_told_to_render_again(monkeypatch):
    render = SimpleNamespace(status=JOB_READY, scene_id=None, notes=[], drawing=None)
    answer = render_answer.of_render(render, _checks(monkeypatch, drawing_version=LIVE - 1))
    assert answer.as_dict()["drawing_version"] == LIVE - 1
    assert f"drawn at drawing {LIVE - 1}, live {LIVE}: render again" in answer.text()
    assert f"drawn at drawing {LIVE - 1}, live {LIVE}: render again" in answer.compact_text()


def test_checks_stored_before_the_field_fall_back_on_the_renders_release(monkeypatch):
    legacy = _checks(monkeypatch)
    legacy.pop("drawing_version")
    drawing = {"release": {"drawing": LIVE - 1, "insert_build": 1}}
    found = render_checks.report_provenance(legacy, None, drawing)
    assert found["drawing_version"] == LIVE - 1 and found["drawing_current"] is False
    assert render_checks.report_provenance(legacy)["drawing_version"] is None


def test_no_checks_no_versions():
    assert render_answer.versions_of(None) == {}
    assert render_answer.versions_line({}) == ""
    answer = render_answer.of_checks(None)
    assert "drawing_version" not in answer.as_dict()


def test_get_video_lists_the_drawing_and_flags_an_older_render(monkeypatch):
    older = _render(checks=_checks(monkeypatch, drawing_version=LIVE - 1))
    line = tool.render_list_line(older, SimpleNamespace(status=jobs.STATE_READY), current=None)
    assert f"screen-insert drawing {LIVE - 1}" in line
    assert f"drawn at drawing {LIVE - 1}, live {LIVE}: render again" in line
    current = _render(checks=_checks(monkeypatch))
    fresh = tool.render_list_line(current, SimpleNamespace(status=jobs.STATE_READY), current=None)
    assert f"screen-insert drawing {LIVE}" in fresh and "render again" not in fresh


def test_get_video_states_the_live_drawing_version(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.261106.0-dev.abc1234")
    line = tool.live_versions_line()
    assert "live build 1.261106.0-dev.abc1234" in line
    assert f"quality report v{quality.QUALITY_REPORT_VERSION}" in line
    assert f"screen-insert drawing {LIVE}" in line
