"""An insert already tracked is never advised ``track: true``, and a render that disagrees with a preview says why
(xtm-one #8453).

A clip scene whose screen insert has ``track: true`` hit ``insert_alignment`` on a 60 fps render with the fix
"give its corners frame by frame (track: true)" - the setting the author had chosen. Its 30 fps preview had passed,
and the warning said "disagrees with preview" without saying the render judged frames the preview never drew.
"""

from __future__ import annotations

from app.services.video_studio import insert_checks, render_checks
from app.services.video_studio.render_checks import INSERT_ALIGNMENT

KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
REFINED = [[0.3, 0.25], [0.7, 0.25], [0.7, 0.75], [0.3, 0.75]]
RENDER_FPS = 60.0
PREVIEW_FPS = 30.0


def _frames(count: int = 30, *, off: range = range(20, 25), excursion: float = 0.0052) -> dict[str, dict]:
    """*count* frames aligned on the bezel, but *off* (five frames at 60 fps) just past the 0.5% gate on its left side."""
    out = {}
    for n in range(count):
        share = excursion if n in off else 0.001
        out[str(n)] = {
            "alignment": share,
            "alignment_sides": {"left": share, "right": 0.001, "top": 0.001, "bottom": 0.001},
            "refined": REFINED,
            "size": [1920, 1080],
        }
    return out


def _placed(*, tracked: bool) -> dict:
    return {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0, tracked=tracked)}


def _alignment(found: list) -> object:
    [finding] = [finding for finding, _proof in found if finding.kind == INSERT_ALIGNMENT]
    return finding


def test_a_tracked_insert_off_its_bezel_is_never_told_to_track_it():
    audits = {KEY: {"frames": _frames(), "evidence": {}}}
    tracked = _alignment(insert_checks.findings(audits, _placed(tracked=True), fps=RENDER_FPS))
    untracked = _alignment(insert_checks.findings(audits, _placed(tracked=False), fps=RENDER_FPS))
    # The same frames, the same gate: the verdict is the gate's, whatever the setting.
    assert tracked.severity == untracked.severity == render_checks.FAIL
    assert tracked.kind == untracked.kind == INSERT_ALIGNMENT
    # Untracked, the advice is the one it always was; tracked, never the setting it already has.
    assert "track: true" in untracked.fix and untracked.fix.startswith("the surface moves under it from t=")
    assert "track: true" not in tracked.fix and "(track: true)" not in tracked.message
    assert tracked.fix != untracked.fix
    # A track starts only on an insert's first frame (#8963): a mid-insert stray is split there, never re-seeded.
    assert tracked.fix.startswith("it is tracked already") and "split it at t=" in tracked.fix
    assert "quad auto" not in tracked.fix
    assert "trim the insert" in tracked.fix and "accepted" in tracked.fix
    assert tracked.data["action"]["kind"] == insert_checks.GIVE_QUAD


def test_a_tracked_insert_whose_bezel_drifts_from_a_lock_is_not_told_to_track_it_either():
    frames = {
        n: {"alignment": 0.001, "alignment_drift": 0.009 if n >= 10 else 0.0, "refined": REFINED, "size": [1920, 1080]}
        for n in range(20)
    }
    at = lambda n: n / RENDER_FPS  # noqa: E731
    off = list(range(10, 20))
    _lead, untracked = insert_checks._alignment_remedy(frames, off, frames[15], at)
    _lead, tracked = insert_checks._alignment_remedy(frames, off, frames[15], at, tracked=True)
    assert "track it (track: true)" in untracked["text"]
    assert "track: true" not in tracked["text"]


def test_a_render_that_disagrees_with_a_preview_at_another_frame_rate_says_so():
    audits = {KEY: {"frames": _frames(), "evidence": {}}}
    # The preview at 30 fps drew every other instant: the excursion fell between its frames, its gate passed.
    previews = render_checks.record_seen(
        None,
        {"s1": "digest-now"},
        "p9",
        checks=render_checks.Checks(inserts=[{"key": KEY, "verdicts": {"alignment": True}, "measures": {"alignment": 0.001}}]).as_dict(),
        revision=3,
        stage="preview",
        fps=PREVIEW_FPS,
    )
    passed = render_checks.insert_verdicts(previews, {"s1": "digest-now"})
    assert passed[KEY][0]["fps"] == PREVIEW_FPS
    finding = _alignment(insert_checks.findings(audits, _placed(tracked=True), fps=RENDER_FPS, passed=passed))
    assert finding.severity == render_checks.WARN and finding.message.startswith("disagrees with preview p9: ")
    cause = finding.data["disagrees"]["cause"]
    assert "30 fps" in cause and "60 fps" in cause and "never drew" in cause
    assert finding.message.endswith(f"they disagree because {cause}")
    assert "track: true" not in finding.fix


def test_a_disagreement_names_its_cause_for_the_save_the_same_rate_and_an_unrecorded_rate():
    assert "footage frame" in insert_checks.disagreement_cause({"stage": "save", "id": "revision 7"}, RENDER_FPS)
    same = insert_checks.disagreement_cause({"stage": "preview", "id": "p9", "fps": 60.0}, RENDER_FPS)
    assert "both judged it at 60 fps" in same and "may not reproduce" in same
    unknown = insert_checks.disagreement_cause({"stage": "preview", "id": "p9"}, RENDER_FPS)
    assert "not recorded" in unknown
