"""A ready render re-judged under the running quality report version, nothing drawn (issue #10885). Fakes only.

* a content finding (a recording scene's ``synthesised_transition``) is placed at its scene's time in the render,
  its frame named, blocking when its severity is;
* the re-judged reports are kept beside the old report, one per version, the old report keys untouched;
* ``report_provenance`` names the re-judge, and its line says it instead of "a new render";
* the answer line names each finding with its time and frame, and what the time bound left unread.
"""

from __future__ import annotations

from app.services.video_studio import quality, render_checks, render_rejudge


def _finding(**values) -> quality.QualityFinding:
    base = {
        "scene_index": 1,
        "scene_id": "s02",
        "scene_type": "recording",
        "label": "Scene 2",
        "kind": "synthesised_transition",
        "severity": "dense",
        "message": "take abcd1234 plays a transition drawn from keyframes",
        "group": "content",
        "data": {"transitions": [{"step": 3}]},
    }
    return quality.QualityFinding(**{**base, **values})


DOCUMENT = {"scenes": [{"id": "s01", "start": 0.0, "end": 4.0}, {"id": "s02", "start": 4.0, "end": 9.5}]}


def test_a_content_finding_is_placed_at_its_scene_with_its_frame():
    placed = render_rejudge._placed(DOCUMENT, _finding(), 30.0)
    assert placed.kind == "synthesised_transition"
    assert placed.at == 4.0 and placed.end == 9.5 and placed.scene_id == "s02"
    assert placed.severity == render_checks.FAIL
    assert placed.data["frame"] == 120
    assert placed.data["transitions"] == [{"step": 3}]


def test_an_advisory_content_finding_is_a_warning():
    assert render_rejudge._placed(DOCUMENT, _finding(severity="advisory"), 30.0).severity == render_checks.WARN


def test_picture_findings_get_their_frame():
    finding = render_checks.Finding("frozen", 2.5, 3.0, "s01", "frozen", render_checks.WARN)
    assert render_rejudge._framed([finding], 25.0)[0].data["frame"] == 62


def test_rejudged_reports_are_kept_beside_the_old_report_one_per_version():
    old = {"quality_report_version": 56, "findings": [], "status": "passed"}
    first = render_rejudge.kept_reports(old, {"quality_report_version": 57, "status": "failed"})
    again = render_rejudge.kept_reports({**old, "rejudged": first}, {"quality_report_version": 57, "status": "passed"})
    assert [r["status"] for r in again] == ["passed"]
    later = render_rejudge.kept_reports({**old, "rejudged": again}, {"quality_report_version": 58, "status": "passed"})
    assert [r["quality_report_version"] for r in later] == [57, 58]
    # The old report is never touched.
    assert old == {"quality_report_version": 56, "findings": [], "status": "passed"}


def test_provenance_names_the_rejudge():
    running = quality.QUALITY_REPORT_VERSION
    checks = {
        "quality_report_version": running - 1,
        "findings": [],
        "rejudged": [
            {
                "quality_report_version": running,
                "status": "failed",
                "findings": [{"kind": "synthesised_transition"}],
                "checked_at": "2026-11-10T18:00:00+00:00",
                "sampled": [],
            }
        ],
    }
    found = render_checks.report_provenance(checks)
    assert found["quality_report_version"] == running - 1 and found["current"] is False
    assert found["rejudged"]["quality_report_version"] == running and found["rejudged"]["current"] is True
    line = render_checks.provenance_line(found)
    assert f"re-judged under v{running}" in line and "nothing drawn" in line


def test_a_report_never_rejudged_points_at_the_rejudge():
    found = render_checks.report_provenance({"quality_report_version": quality.QUALITY_REPORT_VERSION - 1})
    assert "rejudged" not in found
    assert "rejudge=true" in render_checks.provenance_line(found)


def test_the_answer_line_names_findings_frames_and_what_was_not_read():
    line = render_rejudge.rejudge_line(
        {
            "render_id": "81e86eb8",
            "quality_report_version": 57,
            "judged_before": 56,
            "status": "failed",
            "findings": [
                {
                    "kind": "synthesised_transition",
                    "severity": "fail",
                    "at": 4.0,
                    "scene_id": "s02",
                    "message": "drawn from keyframes",
                    "data": {"frame": 120},
                }
            ],
            "sampled": ["1 stretch(es) of the picture watch not judged in time"],
            "stored": True,
        }
    )
    assert "re-judged under quality report v57 (judged under v56), nothing drawn: failed, 1 finding." in line
    assert "at 4.00 s, frame 120, scene s02" in line
    assert "Not read: 1 stretch(es)" in line


async def test_several_renders_share_the_call_budget_and_name_the_rest(monkeypatch):
    budgets: list[float] = []

    async def fake_rejudge(render_id, user_id, *, budget):
        budgets.append(budget)
        return {"render_id": str(render_id), "budget": budget}

    clock = iter([0.0, 0.0, 70.0, 140.0, 140.0])
    monkeypatch.setattr(render_rejudge, "rejudge", fake_rejudge)
    monkeypatch.setattr(render_rejudge, "_clock", lambda: next(clock))
    ids = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 5)]
    judged, left = await render_rejudge.rejudge_many(ids, None, budget=170.0)
    # 170 s for 4 renders: 42.5 s each at first, then the share of what is left; under 30 s the rest waits.
    assert budgets[0] == 42.5 and all(b <= render_rejudge.REJUDGE_BUDGET_SECONDS for b in budgets)
    assert len(judged) == 2 and left == ids[2:]


async def test_one_render_keeps_the_whole_rejudge_budget(monkeypatch):
    async def fake_rejudge(render_id, user_id, *, budget):
        return {"render_id": str(render_id), "budget": budget}

    monkeypatch.setattr(render_rejudge, "rejudge", fake_rejudge)
    judged, left = await render_rejudge.rejudge_many(["00000000-0000-0000-0000-000000000001"], None, budget=170.0)
    assert judged[0]["budget"] == render_rejudge.REJUDGE_BUDGET_SECONDS and left == []


def test_a_render_not_rejudged_says_why():
    assert "not re-judged: only a ready render" in render_rejudge.rejudge_line(
        {"render_id": "x", "error": "only a ready render is re-judged (this one is running)"}
    )
