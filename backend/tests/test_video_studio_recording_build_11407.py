"""A recording names the build that recorded it (#11407).

What is proven: the worker's build is read from its version and commit (``take_state.build_record``), the commit
taken from the development version when ``GIT_SHA`` is not set; the action log's first line starts with the build
that recorded a take, then the one that finished it after a relaunch; a take recorded before the stamp says
"build not recorded" at its end; ``serialize_take`` carries both builds.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import platform_builds
from app.services.video_studio import jobs, take_state

OPENED = {"url": "https://x", "carried": False}


def test_the_build_record_reads_the_version_and_its_commit(monkeypatch):
    monkeypatch.setattr(platform_builds, "running_build", lambda: ("1.261112.0-dev.0fed327", "0FED327ABCDEF0123"))
    assert take_state.build_record() == {"version": "1.261112.0-dev.0fed327", "commit": "0fed327"}
    monkeypatch.setattr(platform_builds, "running_build", lambda: ("1.261112.0-dev.abc1234", ""))
    assert take_state.build_record() == {"version": "1.261112.0-dev.abc1234", "commit": "abc1234"}
    monkeypatch.setattr(platform_builds, "running_build", lambda: ("0.0.0-dev", ""))
    assert take_state.build_record() == {"version": "0.0.0-dev"}


def test_the_first_line_names_the_build_that_recorded_it():
    captured = {"build": {"version": "1.261112.0-dev.0fed327", "commit": "0fed327"}}
    said = take_state.opened_words(
        OPENED,
        worker={"class": "l4", "gpu": "L4"},
        build=take_state.build_words(captured),
        resumed=take_state.resumed_build_words(captured),
    )
    assert said.startswith(
        "recorded on build 1.261112.0-dev.0fed327 (0fed327) by a worker of class l4 (GPU L4), opened on https://x"
    )


def test_a_resumed_take_names_both_builds():
    captured = {
        "build": {"version": "1.261112.0-dev.0fed327", "commit": "0fed327"},
        "resumed_build": {"version": "1.261112.0-dev.1a2b3c4", "commit": "1a2b3c4"},
    }
    said = take_state.opened_words(
        OPENED, build=take_state.build_words(captured), resumed=take_state.resumed_build_words(captured)
    )
    assert said.startswith(
        "recorded on build 1.261112.0-dev.0fed327 (0fed327), "
        "finished on build 1.261112.0-dev.1a2b3c4 (1a2b3c4) after a relaunch, opened on https://x"
    )


def test_an_older_take_says_build_not_recorded():
    said = take_state.opened_words(
        OPENED, worker={"class": "cpu", "gpu": None}, build=take_state.build_words({}), resumed=""
    )
    assert said.startswith("recorded on a worker of class cpu (software GL), opened on https://x")
    assert said.endswith("; build not recorded")
    # A caller that passes no build says nothing of it, as before.
    assert take_state.opened_words(OPENED).startswith("opened on https://x")


def _take(capture):
    return SimpleNamespace(
        id="t",
        user_id=None,
        profile_id=None,
        name="n",
        status="pending",
        progress=0,
        stage=None,
        error=None,
        duration_ms=None,
        viewport_width=None,
        viewport_height=None,
        pixel_ratio=None,
        fps=None,
        captured_frames=None,
        size_bytes=None,
        file_id=None,
        poster_file_id=None,
        contact_sheet_file_id=None,
        script={},
        submitted_script=None,
        capture=capture,
        events=None,
        created_at=None,
        finished_at=None,
    )


def test_serialize_take_carries_both_builds(monkeypatch):
    monkeypatch.setattr(jobs.retention, "retention_fields", lambda take, in_use=False: {})
    build = {"version": "1.261112.0-dev.0fed327", "commit": "0fed327"}
    data = jobs.serialize_take(_take({"build": build}))
    assert data["build"] == build and data["resumed_build"] is None
    assert jobs.serialize_take(_take(None))["build"] is None
