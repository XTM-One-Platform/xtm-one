"""The lab's Video Studio: one video whose current render is ready, so the player, the library's Rendered filter
and the Renders tab render on a real row (#11713).

Started by ``lab.py`` (``seed_video_ready_render``) with the backend's interpreter, from ``backend/`` and with the
backend's environment, once the backend answers: the lab has no render worker and no API writes a finished render,
so the video, its ``video_renders`` row and the two files are written through the backend's own models and file
store. The files are the fixtures under ``fixtures/`` (a ``CLIP_SECONDS`` H.264/AAC MP4 of a test pattern and its
first frame as the poster, 80 KB together), committed with the lab: the ``python-dev`` image has no ffmpeg, and a
seed that needs none cannot be skipped for want of one (#11713 retest). Arguments: the user the video belongs to
and the JSON file the answer is written to.

Found again on a restart: the video titled ``VIDEO_TITLE`` of the user with a current render is answered as it is,
nothing is written twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

VIDEO_TITLE = "Lab sample video with a ready render"
CLIP_SECONDS = 4
WIDTH, HEIGHT, FPS = 1280, 720, 30
QUALITY = "standard"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIDEO_FIXTURE = FIXTURES / "lab-ready-render.mp4"
POSTER_FIXTURE = FIXTURES / "lab-ready-render.jpg"


def spec() -> dict:
    """The free-style video the render draws: one title scene."""
    return {
        "title": VIDEO_TITLE,
        "freestyle": True,
        "scenes": [{"id": "open", "type": "title", "title": "Seeded by the lab: a ready render"}],
    }


def fixtures() -> tuple[Path, Path]:
    """The MP4 and its poster the render points at; a missing one raises, so the lab start stops naming it."""
    for path in (VIDEO_FIXTURE, POSTER_FIXTURE):
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"lab fixture missing or empty: {path}")
    return VIDEO_FIXTURE, POSTER_FIXTURE


async def seed(user_id: str) -> dict:
    """Write the video and its ready current render for *user_id*, or find them again; answer their ids."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.video_studio import JOB_READY, VideoProject, VideoRender
    from app.services.video_studio import assets
    from app.services.video_studio.spec import VideoSpec

    owner = uuid.UUID(user_id)
    async with async_session() as db:
        found = (
            await db.execute(
                select(VideoProject).where(VideoProject.user_id == owner, VideoProject.title == VIDEO_TITLE)
            )
        ).scalars().first()
        if found is not None and found.latest_render_id is not None:
            answer = {"video": str(found.id), "render": str(found.latest_render_id), "found_again": True}
            await engine.dispose()
            return answer

        mp4, poster = fixtures()
        video_file = await assets.store_path(
            str(mp4), filename="lab-ready-render.mp4", user_id=owner, source=assets.SOURCE_RENDER,
            content_type="video/mp4", description="Seeded by the lab: a ready render.",
        )
        poster_file = await assets.store_path(
            str(poster), filename="lab-ready-render.jpg", user_id=owner, source=assets.SOURCE_RENDER,
            content_type="image/jpeg", description="Seeded by the lab: the poster of a ready render.",
        )
        size = mp4.stat().st_size

        if found is None:
            stored = VideoSpec.model_validate(spec()).model_dump(mode="json")
            found = VideoProject(user_id=owner, title=VIDEO_TITLE, spec=stored, scene_count=len(stored["scenes"]))
            db.add(found)
            await db.flush()
        now = datetime.now(UTC)
        render = VideoRender(
            project_id=found.id, user_id=owner, revision=found.revision or 1, quality=QUALITY, status=JOB_READY,
            progress=100, width=WIDTH, height=HEIGHT, fps=FPS, duration_ms=CLIP_SECONDS * 1000, size_bytes=size,
            file_id=video_file.id, poster_file_id=poster_file.id, started_at=now, finished_at=now,
        )
        db.add(render)
        await db.flush()
        found.latest_render_id = render.id
        await db.commit()
        answer = {"video": str(found.id), "render": str(render.id), "found_again": False}
    await engine.dispose()
    return answer


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
