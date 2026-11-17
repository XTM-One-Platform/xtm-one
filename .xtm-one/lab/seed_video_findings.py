"""The lab's Video Studio renders: a whole render and a scene preview whose findings list renders on real rows.

Started by ``lab.py`` (``seed_video_findings``) with the backend's interpreter, from ``backend/`` and with the
backend's environment, once the backend answers and a video exists: the lab has no render worker, and no API
writes a finished render, so the ``video_renders`` rows are written through the backend's own model.
Arguments: the video (created by ``lab.py`` through the API), the user the renders belong to, the JSON file
the render ids are written to and, for a render gate video, ``--gate`` (or ``--gate-one``) and the kinds to refuse on
(comma-separated).

* one whole render (``failed``) per group of ``KINDS_PER_RENDER`` kinds a render's checks write
  (``render_checks``: what ``FINDING_KIND_LABELS`` in ``video-format.ts`` labels), one finding of each severity
  (``fail``, ``warn``) per kind: the Renders tab lists the first ``VISIBLE_FINDINGS`` findings of a render, so
  every kind renders on a real row without a fetch override;
* the scene preview of the video's first scene (``failed``): one ``fail`` and one ``warn``;
* a kind a check writes with a ``code`` (``CODED_KINDS``: the keys of ``FINDING_MESSAGES`` in ``video-format.ts``)
  carries that code and plausible ``params``, so the editor words it in the reader's language; the English
  ``message`` stays as the fallback, and the other kinds carry none, as the checks write them.

With ``--gate``: one whole render (``failed``) only, its findings one open ``fail`` per gate kind (default: every
``PICTURE_KINDS`` kind) on the first scene, so the quality gate refuses the next render on them (``open_findings``
reads the latest whole render of the revision alone) and the editor's refused-render dialog lists them.

With ``--gate-one``: the same, on the first of those kinds alone, so the dialog's reason renders its singular form
("1 finding") on a real row.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

SEEDED_BY = "Seeded by the lab"
# ``RenderFindings`` in ``RendersPanel.tsx`` shows ``findings.slice(0, 6)`` then "and N more".
VISIBLE_FINDINGS = 6
KINDS_PER_RENDER = VISIBLE_FINDINGS // 2
#: The kinds whose check writes ``code=<kind>`` and ``params`` (``frame_freeze``, ``brightness_dip``,
#: ``double_frames``/``double_take``/``cut_blend``, ``near_empty``, ``static_chrome``): the keys of
#: ``FINDING_MESSAGES``.
CODED_KINDS = ("brightness_dip", "double_image", "frozen", "near_empty", "static_flicker")
#: How long a seeded coded finding lasts when its row has no ``end``: the brief's ``near_empty`` example.
CODED_SECONDS = 1.7


def finding_kinds() -> list[str]:
    """Every kind a render's checks write, sorted: the keys ``FINDING_KIND_LABELS`` must label."""
    from app.services.video_studio import render_checks

    return sorted(
        render_checks.AUDIO_KINDS
        | render_checks.PICTURE_KINDS
        | render_checks.HELD_WARNINGS
        | {render_checks.MUSIC_LEVEL, render_checks.AUDIO_MIX}
    )


def coded_params(kind: str, severity: str, at: float, end: float | None) -> dict:
    """The ``params`` the check of *kind* writes, with the same keys and value types, for a span from *at*."""
    from app.services.video_studio.render_checks import clock

    stop = end if end is not None else round(at + CODED_SECONDS, 3)
    span = {"start": clock(at), "end": clock(stop)}
    if kind in ("frozen", "near_empty"):
        return {"seconds": round(stop - at, 1), **span}
    if kind == "brightness_dip":
        return {"share": "42", **span}
    if kind == "double_image":
        # Both plural forms: two frames on the fail row, one on the warn row.
        return {"frames": 2 if severity == "fail" else 1, **span}
    if kind == "static_flicker":
        return {"frames": 4, **span}
    raise ValueError(f"no params for {kind}")


def findings(kinds: list[str], scene_id: str | None) -> list[dict]:
    """One ``fail`` and one ``warn`` finding per kind, half a second apart, the second one ending; a kind of
    ``CODED_KINDS`` carries its ``code`` and ``params``."""
    out: list[dict] = []
    for index, kind in enumerate(kinds):
        for offset, severity in enumerate(("fail", "warn")):
            at = round(index + offset * 0.5, 3)
            end = round(at + 0.4, 3) if offset else None
            row = {
                "kind": kind, "severity": severity, "at": at, "end": end,
                "scene_id": scene_id, "message": f"{SEEDED_BY}: a {severity} {kind} finding.",
            }
            if kind in CODED_KINDS:
                row["code"] = kind
                row["params"] = coded_params(kind, severity, at, end)
            out.append(row)
    return out


def gate_kinds(requested: str = "") -> list[str]:
    """The kinds the gate video's render refuses on: *requested* (comma-separated, ``LAB_VIDEO_GATE_KINDS``) in its
    order, else every ``PICTURE_KINDS`` kind sorted. A kind the gate does not refuse on as a ``fail`` raises."""
    from app.services.video_studio import render_checks

    refusing = render_checks.AUDIO_KINDS | render_checks.PICTURE_KINDS
    kinds = [k.strip() for k in requested.split(",") if k.strip()] or sorted(render_checks.PICTURE_KINDS)
    unknown = [k for k in kinds if k not in refusing]
    if unknown:
        raise ValueError(f"not a kind the render gate refuses on as a fail: {', '.join(unknown)}")
    return list(dict.fromkeys(kinds))


def gate_findings(kinds: list[str], scene_id: str | None) -> list[dict]:
    """One open ``fail`` finding per kind of *kinds*, a second apart, on *scene_id*: what ``open_findings`` keeps."""
    return [
        {
            "kind": kind, "severity": "fail", "at": float(index), "end": round(index + 0.4, 3),
            "scene_id": scene_id, "message": f"{SEEDED_BY}: a fail {kind} finding the render gate refuses on.",
        }
        for index, kind in enumerate(kinds)
    ]


def kind_groups(kinds: list[str]) -> list[list[str]]:
    """*kinds* in groups of ``KINDS_PER_RENDER``: one whole render each, every finding of it visible."""
    return [kinds[i:i + KINDS_PER_RENDER] for i in range(0, len(kinds), KINDS_PER_RENDER)]


async def seed(project_id: str, user_id: str, gate: list[str] | None = None) -> dict:
    """Write the whole renders and the scene preview of *project_id*; answer their ids and kinds. With *gate*, the
    single whole render whose open ``fail`` findings of those kinds the quality gate refuses the next render on."""
    import app.models  # noqa: F401 - every table, so the renders' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.video_studio import JOB_FAILED, VideoProject, VideoRender

    kinds = finding_kinds()
    now = datetime.now(UTC)
    async with async_session() as db:
        project = await db.get(VideoProject, uuid.UUID(project_id))
        if project is None:
            raise RuntimeError(f"no video {project_id}")
        scenes = [s for s in (project.spec or {}).get("scenes") or [] if isinstance(s, dict) and s.get("id")]
        scene_id = str(scenes[0]["id"]) if scenes else None

        def render(scene: str | None, rows: list[dict]) -> VideoRender:
            return VideoRender(
                project_id=project.id, user_id=uuid.UUID(user_id), revision=project.revision, quality="draft",
                status=JOB_FAILED, progress=100, scene_id=scene, error=f"{SEEDED_BY}: the checks failed it.",
                checks={"findings": rows}, started_at=now, finished_at=now,
            )

        if gate is not None:
            refused = render(None, gate_findings(gate, scene_id))
            db.add(refused)
            await db.commit()
            ids: dict = {"gate": {"id": str(refused.id), "kinds": gate}}
        else:
            ids = await seed_findings(db, render, kinds, scene_id)
    await engine.dispose()
    return ids


async def seed_findings(db, render, kinds: list[str], scene_id: str | None) -> dict:
    """The whole renders, one per kind group, and the scene preview of the findings video; their ids and kinds."""
    groups = kind_groups(kinds)
    wholes = [render(None, findings(group, scene_id)) for group in groups]
    preview = render(scene_id, findings(kinds[:1], scene_id)) if scene_id else None
    db.add_all([*wholes, *([preview] if preview is not None else [])])
    await db.commit()
    return {
        "renders": [{"id": str(row.id), "kinds": group} for row, group in zip(wholes, groups, strict=True)],
        "preview": str(preview.id) if preview else "",
    }


def main() -> None:
    project_id, user_id, out = sys.argv[1:4]
    flag = sys.argv[4] if len(sys.argv) > 4 else ""
    gate = gate_kinds(sys.argv[5] if len(sys.argv) > 5 else "") if flag in ("--gate", "--gate-one") else None
    if gate is not None and flag == "--gate-one":
        gate = gate[:1]
    Path(out).write_text(json.dumps(asyncio.run(seed(project_id, user_id, gate))))


if __name__ == "__main__":
    main()
