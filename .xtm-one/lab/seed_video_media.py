"""The lab's Video Studio media library: recordings and AI shots, some used by a video, on real rows.

Started by ``lab.py`` (``seed_video_media``) with the backend's interpreter, from ``backend/`` and with the
backend's environment, once the backend answers: the lab has no recorder and no video model, and no API writes a
finished recording or shot, so the ``video_takes`` and ``video_clips`` rows (and the video that uses some of them)
are written through the backend's own models. Arguments: the user the media belong to and the JSON file the
answer is written to.

* ``TAKES`` ready recordings and ``CLIPS`` ready AI shots, ``MEDIA`` in all: more than two pages of the Media tab
  (``library.ASSET_PAGE_SIZE``), so its toolbar, paging and selection render on real rows;
* their ``created_at`` spread over the last ``SPREAD_DAYS`` days, inside the unused retention
  (``VIDEO_STUDIO_UNUSED_MEDIA_RETENTION_DAYS``, 14 by default), so none expires during a lab's lifetime;
* one free-style video whose recording and clip scenes name every ``USED_EVERY``-th medium: ``in_use`` true and
  ``used_in`` naming it on those, the rest unused, so ``?usage=used`` and ``?usage=unused`` both list rows;
* one recording with a name of ``LONG_NAME_LENGTH`` characters, for the card's truncation.

Found again on a restart: the media named ``SEED_PREFIX`` of the user are counted, and none is written twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED_PREFIX = "Lab media"
TAKES = 36
CLIPS = 28
MEDIA = TAKES + CLIPS
#: Every third medium (indexes 0, 3, 6...) is used by the seeded video: about a third of the library.
USED_EVERY = 3
SPREAD_DAYS = 6
LONG_NAME_LENGTH = 160
VIDEO_TITLE = "Lab sample video media library"


def media_plan(now: datetime) -> list[dict]:
    """The ``MEDIA`` rows to write, newest first: kind, name, created_at and whether the video uses it."""
    step = timedelta(days=SPREAD_DAYS) / MEDIA
    # Interleaved: 9 recordings then 7 shots in every 16 rows, so 64 rows hold 36 and 28 and every page both kinds.
    rows = [
        {
            "kind": "take" if index % 16 < 9 else "clip",
            "created_at": now - step * index,
            "used": index % USED_EVERY == 0,
        }
        for index in range(MEDIA)
    ]
    counts = {"take": 0, "clip": 0}
    for row in rows:
        counts[row["kind"]] += 1
        what = "recording" if row["kind"] == "take" else "AI shot"
        row["name"] = f"{SEED_PREFIX} {what} {counts[row['kind']]:02d}"
    long_name = f"{SEED_PREFIX} recording with a very long name: the Video Studio Media tab truncates it on its card "
    first_take = next(row for row in rows if row["kind"] == "take" and not row["used"])
    first_take["name"] = (long_name + "and keeps the whole name in its title " * 4)[:LONG_NAME_LENGTH].rstrip()
    return rows


def video_spec(used: list[tuple[str, str]]) -> dict:
    """A free-style video naming each ``(kind, id)`` of *used* in one scene: a recording scene or a clip scene."""
    scenes: list[dict] = [{"id": "open", "type": "title", "title": "Seeded by the lab"}]
    for index, (kind, asset_id) in enumerate(used):
        if kind == "take":
            scenes.append({"id": f"take-{index}", "type": "recording", "take_id": asset_id})
        else:
            scenes.append({"id": f"clip-{index}", "type": "clip", "clip_id": asset_id})
    return {"title": VIDEO_TITLE, "freestyle": True, "scenes": scenes}


async def seed(user_id: str) -> dict:
    """Write the media library of *user_id* and the video using a third of it; answer the counts and the video."""
    from sqlalchemy import func, select

    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.video_studio import JOB_READY, VideoClip, VideoProject, VideoTake
    from app.services.video_studio.spec import VideoSpec

    owner = uuid.UUID(user_id)
    now = datetime.now(UTC)
    async with async_session() as db:
        found = 0
        for model in (VideoTake, VideoClip):
            found += (
                await db.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.user_id == owner, model.name.startswith(SEED_PREFIX))
                )
            ).scalar_one()
        video = (
            (
                await db.execute(
                    select(VideoProject.id).where(VideoProject.user_id == owner, VideoProject.title == VIDEO_TITLE)
                )
            )
            .scalars()
            .first()
        )
        if found:
            await engine.dispose()
            return {"media": found, "used": 0, "video": str(video) if video else "", "found_again": True}

        used: list[tuple[str, str]] = []
        for row in media_plan(now):
            if row["kind"] == "take":
                media = VideoTake(
                    user_id=owner,
                    name=row["name"],
                    status=JOB_READY,
                    progress=100,
                    script={"steps": []},
                    duration_ms=12_000,
                    viewport_width=1440,
                    viewport_height=900,
                    fps=30,
                    created_at=row["created_at"],
                    finished_at=row["created_at"],
                )
            else:
                media = VideoClip(
                    user_id=owner,
                    name=row["name"],
                    provider="lab",
                    model="lab-stub",
                    prompt=f"{row['name']}: a slow pan over a security operations dashboard.",
                    params={},
                    status=JOB_READY,
                    progress=100,
                    duration_ms=6_000,
                    width=1280,
                    height=720,
                    created_at=row["created_at"],
                    finished_at=row["created_at"],
                )
            db.add(media)
            await db.flush()
            if row["used"]:
                used.append((row["kind"], str(media.id)))
        # Stored as the API stores a video: the validated spec, dumped in JSON mode.
        spec = VideoSpec.model_validate(video_spec(used)).model_dump(mode="json")
        project = VideoProject(user_id=owner, title=VIDEO_TITLE, spec=spec, scene_count=len(spec["scenes"]))
        db.add(project)
        await db.commit()
        answer = {"media": MEDIA, "used": len(used), "video": str(project.id), "found_again": False}
    await engine.dispose()
    return answer


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
