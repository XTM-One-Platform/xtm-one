# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A ready render judged again under the running quality report version, nothing drawn (issue #10885).

When the checker moves to a new ``quality.QUALITY_REPORT_VERSION``, a ready render keeps the report of the version
that judged it, and a new render is refused by the gate while the new checks find something. :func:`rejudge` runs the
running version's checks on what the render already is:

* the picture checks on the render's own encoded frames, read from its stored MP4 (``frame_freeze.check_frames``:
  frozen pictures, two pictures at once, a recording canvas left near empty, static chrome drawn twice, brightness
  dips; ``cut_blend.read_cuts``: a frame of both scenes at a cut), within a bounded time - a stretch not judged in it
  is said, never passed in silence;
* the content checks on the spec of the render's own revision (``quality.check_content``): the take-level findings of
  its recording scenes (a transition synthesised from keyframes or held then cut, a take captured too slowly...), each
  placed at its scene's time in the render, with its frame.

Read-only on the MP4: no frame is drawn, no render is queued. The report is stored beside the one the render was
judged with (``VideoRender.checks["rejudged"]``, one entry per quality report version, the newest last): the old
report stays as it was, and :func:`render_checks.report_provenance` names the re-judge.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The wall-clock seconds a re-judge may take, the picture watch included: inside one tool call.
REJUDGE_BUDGET_SECONDS = 150.0
#: The share of the budget the content checks may take, so the picture watch keeps the rest.
CONTENT_SHARE = 0.4
#: The fewest seconds a render of a several-render call is given: below it, the render is left for a further call.
REJUDGE_MIN_SECONDS = 30.0
#: The re-judged reports a render keeps, one per quality report version: the newest.
REJUDGED_KEPT = 5
#: A quality finding of these severities is a failure of the re-judged report, as the render gate counts it.
_FAILING = ("overloaded", "dense")
#: The clock :func:`rejudge_many` shares its budget by (a seam for the tests, the event loop's clock left alone).
_clock = time.monotonic


def _placed(document: dict, finding: Any, fps: float) -> Any:
    """A content finding (``quality.QualityFinding``) as a render finding at its scene's place in *document*."""
    from app.services.video_studio import render_checks

    scene = next(
        (s for s in document.get("scenes") or [] if isinstance(s, dict) and s.get("id") == finding.scene_id), None
    )
    at = float((scene or {}).get("start") or 0.0)
    end = float(scene["end"]) if scene and scene.get("end") is not None else None
    data = dict(finding.data or {})
    data.update({"frame": round(at * fps) if fps else None, "quality_kind": finding.kind, "group": finding.group})
    return render_checks.Finding(
        finding.kind,
        at,
        end,
        finding.scene_id,
        finding.message,
        render_checks.FAIL if finding.severity in _FAILING else render_checks.WARN,
        data,
    )


def _framed(findings: list, fps: float) -> list:
    """Each finding with the frame of the MP4 it starts on (``data.frame``)."""
    from dataclasses import replace

    out = []
    for finding in findings:
        if fps and "frame" not in (finding.data or {}):
            finding = replace(finding, data={**(finding.data or {}), "frame": round(finding.at * fps)})
        out.append(finding)
    return out


def kept_reports(checks: dict | None, report: dict) -> list[dict]:
    """The re-judged reports *checks* keeps once *report* is added: one per quality report version (a re-judge at a
    version already kept replaces it), the newest :data:`REJUDGED_KEPT`, oldest first."""
    earlier = [
        r
        for r in ((checks or {}).get("rejudged") or [])
        if isinstance(r, dict) and r.get("quality_report_version") != report.get("quality_report_version")
    ]
    return [*earlier, report][-REJUDGED_KEPT:]


def latest(checks: dict | None) -> dict | None:
    """The newest re-judged report *checks* keeps; ``None`` when it was never re-judged."""
    reports = [r for r in ((checks or {}).get("rejudged") or []) if isinstance(r, dict)]
    return reports[-1] if reports else None


async def rejudge(render_id: uuid.UUID, user_id: uuid.UUID, *, budget: float = REJUDGE_BUDGET_SECONDS) -> dict:
    """Judge the ready render *render_id* again under the running quality report version, as *user_id* (who may
    read it; the report is stored only when they may manage its video). Never raises: what could not run is said.

    Returns ``{render_id, quality_report_version, judged_before, status, findings, frames, cuts, notes, stored,
    sampled}``: *findings* each with its time and frame in the MP4, *sampled* naming what the time bound left unread.
    """
    from app.core.database import async_session
    from app.models.user import User
    from app.models.video_studio import JOB_READY, VideoProject, VideoRender
    from app.services.video_studio import assets, cut_blend, frame_freeze, projects, quality, render_checks, revisions

    started = time.monotonic()
    notes: list[str] = []
    async with async_session() as db:
        render = await db.get(VideoRender, render_id)
        user = await db.get(User, user_id)
        project = await db.get(VideoProject, render.project_id) if render is not None else None
        if render is None or user is None or project is None:
            return {"render_id": str(render_id), "error": "render not found"}
        if render.status != JOB_READY or render.file_id is None:
            return {"render_id": str(render_id), "error": f"only a ready render is re-judged (this one is {render.status})"}
        judged_before = render_checks._int_or_none((render.checks or {}).get("quality_report_version"))
        may_store = await projects.can_manage_project(db, project, user)
        try:
            authored = (
                dict(project.spec or {})
                if render.revision == project.revision
                else await revisions.spec_at(db, project.id, render.revision, current=project.revision)
            )
        except revisions.RevisionNotFoundError as exc:
            # Its revision is no longer held (pruned, or saved before the history): a held revision that draws and
            # plays exactly what the render recorded is its spec as well (issue #10899).
            found = await same_drawing_spec(db, project, user, render)
            if found is None:
                return {
                    "render_id": str(render_id),
                    "error": (
                        f"the spec it was rendered from is gone: {exc} No held revision draws and plays what it "
                        "recorded: render the video again to judge it under the running checker."
                    ),
                }
            authored, matched = found
            notes.append(
                f"Revision {render.revision} is no longer held: judged on revision {matched}, which draws and plays "
                "exactly what this render recorded."
            )
        applied = await projects.effective_spec(db, project, user, authored=authored)
        placed = await projects.measured_plan(db, applied.spec, user)
        document = placed.plan.document
        fps = float(render.fps or document.get("fps") or 30)
        size = (int(render.width or document.get("width") or 1920), int(render.height or document.get("height") or 1080))
        seconds = (render.duration_ms or 0) / 1000.0 or float(placed.plan.duration)
        scene_id = render.scene_id
        file_id = render.file_id
    if abs(seconds - float(placed.plan.duration)) > 0.5 and scene_id is None:
        notes.append(
            f"The revision plans {placed.plan.duration:.1f} s and the MP4 plays {seconds:.1f} s: scene times are read "
            "from the plan as the save measures it now."
        )
    checks = render_checks.Checks()
    sampled: list[str] = []

    # The content checks of the render's own revision: the take-level findings of its recording scenes included.
    # Bounded like the picture watch (they may read shots and media through the vision provider): what the bound
    # leaves unjudged is said under ``sampled``, never passed in silence.
    content_limit = max(5.0, (budget - (time.monotonic() - started)) * CONTENT_SHARE)
    try:
        content = await asyncio.wait_for(
            quality.check_content(project.id, user_id, music=False, read_frames=False, authored=authored),
            timeout=content_limit,
        )
    except TimeoutError:
        content = None
        sampled.append(f"the content checks did not finish within {content_limit:.0f} s")
    except Exception as exc:  # said, never a failed re-judge
        content = None
        sampled.append(f"the content checks could not run: {format_exception_message(exc)[:160]}")
    if content is not None:
        shown = {scene_id} if scene_id else None
        checks.findings.extend(
            _placed(document, finding, fps)
            for finding in content.findings
            if shown is None or finding.scene_id in shown or finding.scene_id is None
        )
        checks.notes.extend(content.notes)
    if scene_id is not None:
        # A scene preview plays a window of its own plan: its frames are not timed on the whole video's.
        sampled.append("a scene preview: its frames are not watched again, its scene's content findings only")
    else:
        with tempfile.TemporaryDirectory(prefix="video-rejudge-") as workdir:
            path = str(Path(workdir) / "render.mp4")
            if await assets.download_file(file_id, path) is None:
                sampled.append("its MP4 is gone from the store: no frame watched")
            else:
                left = max(10.0, budget - (time.monotonic() - started))
                try:
                    frames = await asyncio.wait_for(
                        frame_freeze.check_frames(
                            path,
                            document=document,
                            width=size[0],
                            height=size[1],
                            fps=fps,
                            duration=seconds,
                            budget=frame_freeze.window_budget(left / 4),
                        ),
                        timeout=left * 0.75,
                    )
                    checks.frames = frames.as_summary()
                    checks.findings.extend(frames.findings)
                    if unjudged := checks.frames.get("unjudged"):
                        sampled.append(f"{len(unjudged)} stretch(es) of the picture watch not judged in time")
                except TimeoutError:
                    checks.frames = {"status": render_checks.SKIPPED, "reason": f"not watched within {left * 0.75:.0f} s"}
                    sampled.append(f"the picture watch did not finish within {left * 0.75:.0f} s")
                except Exception as exc:  # said, never a failed re-judge
                    checks.frames = {"status": render_checks.SKIPPED, "reason": format_exception_message(exc)[:200]}
                try:
                    left = max(5.0, budget - (time.monotonic() - started))
                    cuts = await asyncio.wait_for(cut_blend.read_cuts(path, document, size=size), timeout=left)
                    checks.cuts = cuts.as_summary(read_on="stored MP4, re-judged")
                    checks.findings.extend(cuts.findings)
                except TimeoutError:
                    checks.cuts = cut_blend.not_read("not read within the re-judge's time")
                    sampled.append("the cut joins were not read in time")
                except Exception as exc:
                    checks.cuts = cut_blend.not_read(format_exception_message(exc)[:160])
    checks.findings = _framed(checks.findings, fps)
    checks.notes.extend(notes)
    report = {
        **checks.as_dict(),
        "rejudged_from": judged_before,
        "sampled": sampled,
        "seconds": round(time.monotonic() - started, 1),
    }
    stored = False
    if may_store:
        async with async_session() as db:
            row = await db.get(VideoRender, render_id)
            if row is not None:
                # A new dict: the old report keys stay as they were, the re-judge beside them.
                row.checks = {**(row.checks or {}), "rejudged": kept_reports(row.checks, report)}
                await db.commit()
                stored = True
    else:
        notes.append("Not stored: you may read this video, not manage it.")
    return {
        "render_id": str(render_id),
        "quality_report_version": report["quality_report_version"],
        "judged_before": judged_before,
        "status": report["status"],
        "findings": report["findings"],
        "frames": report["frames"],
        "cuts": report["cuts"],
        "notes": report["notes"],
        "sampled": sampled,
        "stored": stored,
        "seconds": report["seconds"],
    }


#: The held revisions :func:`same_drawing_spec` compares, the current one first, then the oldest held.
SAME_DRAWING_TRIES = 6


async def same_drawing_spec(db: Any, project: Any, user: Any, render: Any) -> tuple[dict, int] | None:
    """``(authored spec, revision)`` of a held revision of *project* that draws and plays exactly what the whole
    *render* recorded (``VideoRender.drawing``: every scene's drawn digest and what it plays beside its frames), the
    current revision first, then the oldest held (the nearest to the render's); ``None`` when the render recorded
    neither or none of :data:`SAME_DRAWING_TRIES` matches."""
    from app.services.video_studio import lexicon_store, projects, render_checks, revisions
    from app.services.video_studio.spec import parse_video_spec

    drawing = render.drawing if isinstance(getattr(render, "drawing", None), dict) else {}
    if render.scene_id is not None or not drawing.get("scenes") or not drawing.get("heard"):
        return None
    tries = [project.revision]
    held = await revisions.held_range(db, project.id)
    if held is not None:
        tries += [r for r in range(held[0], held[1] + 1) if r != project.revision][: SAME_DRAWING_TRIES - 1]
    for revision in tries:
        try:
            authored = (
                dict(project.spec or {})
                if revision == project.revision
                else await revisions.spec_at(db, project.id, revision, current=project.revision)
            )
            effective = (await projects.effective_spec(db, project, user, authored=authored)).spec
            lexicon = await lexicon_store.for_voiceover(db, parse_video_spec(effective).voiceover)
            marks = await render_checks.digest_scenes(db, effective, lexicon)
        except Exception as exc:  # one revision that cannot be read leaves the others to compare
            logger.info("Revision %s of video %s not compared: %s", revision, project.id, format_exception_message(exc))
            continue
        if marks.drawn == drawing.get("scenes") and marks.heard == drawing.get("heard"):
            return authored, revision
    return None


async def rejudge_many(render_ids: list[str], user_id: uuid.UUID, *, budget: float) -> tuple[list[dict], list[str]]:
    """Re-judge *render_ids* one after another within *budget* seconds in all: each render is given its share of the
    time left (at most :data:`REJUDGE_BUDGET_SECONDS`), and once that share falls under :data:`REJUDGE_MIN_SECONDS`
    the renders not reached are returned, for a further call, instead of overrunning the call's deadline.

    Returns ``(judged, left)``: the :func:`rejudge` results, and the ids not re-judged in this call.
    """
    started = _clock()
    judged: list[dict] = []
    for index, rid in enumerate(render_ids):
        share = (budget - (_clock() - started)) / (len(render_ids) - index)
        if share < REJUDGE_MIN_SECONDS:
            if judged:
                return judged, list(render_ids[index:])
            # The first render is always judged, on the time the call has.
            share = max(share, REJUDGE_MIN_SECONDS)
        judged.append(await rejudge(uuid.UUID(rid), user_id, budget=min(REJUDGE_BUDGET_SECONDS, share)))
    return judged, []


def rejudge_line(result: dict) -> str:
    """What ``get_video_jobs(rejudge=true)`` says of one re-judge (:func:`rejudge`)."""
    if result.get("error"):
        return f"Render `{result['render_id']}` not re-judged: {result['error']}."
    findings = result.get("findings") or []
    before = result.get("judged_before")
    head = (
        f"Render `{result['render_id']}` re-judged under quality report v{result['quality_report_version']}"
        f"{f' (judged under v{before})' if before is not None else ''}, nothing drawn: {result['status']}, "
        f"{len(findings)} finding{'s' if len(findings) != 1 else ''}"
    )
    lines = [head + ("." if result.get("stored") else " (not stored).")]
    for finding in findings[:12]:
        frame = (finding.get("data") or {}).get("frame")
        lines.append(
            f"- {finding['kind']} ({finding['severity']}) at {finding['at']:.2f} s"
            f"{f', frame {frame}' if frame is not None else ''}, scene {finding.get('scene_id') or '-'}: "
            f"{finding['message'][:240]}"
        )
    if len(findings) > 12:
        lines.append(f"- and {len(findings) - 12} more in quality_report.rejudged")
    if result.get("sampled"):
        lines.append("Not read: " + "; ".join(result["sampled"]) + ".")
    return "\n".join(lines)
