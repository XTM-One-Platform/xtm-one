"""A render's quality report says which checker judged it (dev:video-jobs-quality-report-version). Fakes only.

* the checks a render stores carry ``quality_report_version``, ``build`` and ``checked_at``; read back
  (``Checks.from_dict``) they keep the values they were judged with, the findings' format unchanged;
* ``report_provenance`` tells a report from the running checker from an older one, and a render resumed across
  builds (its relaunches name another release) judged again, every frame, by the build that finished it;
* ``get_video``'s render list says it on an ended render, and says nothing on a render with no checks.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.core.config import settings
from app.models.video_studio import JOB_READY, VideoRender
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import jobs, quality, render_checks


def _stored(monkeypatch, build: str = "1.261105.0-dev.abc1234") -> dict:
    monkeypatch.setattr(settings, "app_version", build)
    finding = render_checks.Finding(kind="frozen", at=1.0, end=2.0, scene_id="s01", message="frozen", severity="warn")
    return render_checks.Checks(findings=[finding]).as_dict()


def test_stored_checks_name_their_version_build_and_time(monkeypatch):
    stored = _stored(monkeypatch)
    assert stored["quality_report_version"] == quality.QUALITY_REPORT_VERSION
    assert stored["build"] == "1.261105.0-dev.abc1234"
    assert stored["checked_at"]
    # The findings' format is unchanged.
    assert stored["findings"] == [
        {"kind": "frozen", "severity": "warn", "at": 1.0, "end": 2.0, "scene_id": "s01", "message": "frozen"}
    ]


def test_checks_read_back_keep_the_checker_that_judged_them(monkeypatch):
    stored = {
        **_stored(monkeypatch),
        "quality_report_version": 3,
        "build": "1.0.0-old",
        "checked_at": "2026-11-01T00:00:00+00:00",
    }
    monkeypatch.setattr(settings, "app_version", "9.9.9-new")
    again = render_checks.Checks.from_dict(stored).as_dict()
    assert again["quality_report_version"] == 3
    assert again["build"] == "1.0.0-old"
    assert again["checked_at"] == "2026-11-01T00:00:00+00:00"


def test_a_report_from_the_running_checker_is_current(monkeypatch):
    found = render_checks.report_provenance(_stored(monkeypatch))
    assert found is not None
    assert set(found) == {
        "quality_report_version",
        "drawing_version",
        "live_drawing_version",
        "drawing_current",
        "build",
        "checked_at",
        "current",
        "resumed_across_builds",
        "recomputed_after_resume",
    }
    assert found["current"] is True
    assert found["recomputed_after_resume"] is False
    assert "(current)" in render_checks.provenance_line(found)


def test_a_report_from_an_older_checker_is_not_current():
    old = {"quality_report_version": quality.QUALITY_REPORT_VERSION - 1, "findings": []}
    found = render_checks.report_provenance(old)
    assert found["current"] is False
    assert found["build"] is None and found["checked_at"] is None
    line = render_checks.provenance_line(found)
    assert "build not recorded" in line and "older than the running checker" in line


def test_a_render_resumed_across_a_deploy_is_judged_again_by_the_build_that_finished_it(monkeypatch):
    stored = _stored(monkeypatch, build="1.261105.0-dev.new0001")
    relaunches = [
        {"attempt": 2, "frames_kept": 900, "version": "1.261104.0-dev.old0001"},
        {"attempt": 3, "frames_kept": 1800, "version": "1.261104.0-dev.old0001"},
        {"attempt": 4, "frames_kept": 2400, "version": "1.261105.0-dev.new0001"},
    ]
    found = render_checks.report_provenance(stored, relaunches)
    assert found["resumed_across_builds"] == ["1.261104.0-dev.old0001"]
    assert found["recomputed_after_resume"] is True
    assert found["current"] is True
    assert "resumed across builds 1.261104.0-dev.old0001" in render_checks.provenance_line(found)


def test_no_checks_no_provenance():
    assert render_checks.report_provenance(None) is None
    assert render_checks.report_provenance({}) is None
    assert render_checks.provenance_line(None) == ""


def _render(**values) -> VideoRender:
    fields = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": JOB_READY,
        "quality": "ultra",
        "revision": 3,
        "width": 3840,
        "height": 2160,
        "fps": 60,
        "duration_ms": 60_000,
        "file_id": None,
        "scene_id": None,
        "keep": False,
    }
    return VideoRender(**{**fields, **values})


def test_get_video_lists_which_checker_judged_an_ended_render(monkeypatch):
    judged = _render(checks=_stored(monkeypatch))
    line = tool.render_list_line(judged, SimpleNamespace(status=jobs.STATE_READY), current=None)
    assert f"checks v{quality.QUALITY_REPORT_VERSION} by build 1.261105.0-dev.abc1234" in line
    unjudged = _render(checks=None)
    assert "checks v" not in tool.render_list_line(unjudged, SimpleNamespace(status=jobs.STATE_READY), current=None)
